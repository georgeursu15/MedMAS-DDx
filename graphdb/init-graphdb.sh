#!/bin/sh

set -eu

GRAPHDB_URL="${GRAPHDB_URL:-http://graphdb:7200}"
REPOS_ENDPOINT="${GRAPHDB_URL}/rest/repositories"
LICENSE_HINT_URL="https://www.ontotext.com/products/graphdb/#try-graphdb"

echo "[graphdb-init] Waiting for GraphDB at ${GRAPHDB_URL} ..."
until curl -fsS "${REPOS_ENDPOINT}" >/dev/null 2>&1; do
  sleep 2
done
echo "[graphdb-init] GraphDB is reachable"

show_license_hint_and_exit() {
  echo "[graphdb-init] ERROR: GraphDB license is missing or invalid." >&2
  echo "[graphdb-init] GraphDB 10+ requires a Free or Enterprise license file." >&2
  echo "[graphdb-init] Request a GraphDB Free license: ${LICENSE_HINT_URL}" >&2
  echo "[graphdb-init] Place it at ./graphdb/license/graphdb.license and rerun docker compose up -d --build." >&2
  exit 1
}

request_with_status() {
  output_file="$1"
  shift
  curl -sS -o "${output_file}" -w "%{http_code}" "$@"
}

handle_failed_graphdb_call() {
  http_code="$1"
  output_file="$2"
  operation="$3"

  if grep -qiE "No license was set|License validation has failed|graphdb\.license" "${output_file}"; then
    rm -f "${output_file}"
    show_license_hint_and_exit
  fi

  echo "[graphdb-init] ERROR: ${operation} failed (HTTP ${http_code})." >&2
  sed -n '1,20p' "${output_file}" >&2
  rm -f "${output_file}"
  exit 1
}

repo_exists() {
  repo_id="$1"
  curl -fsS "${REPOS_ENDPOINT}" | grep -q '"id"[[:space:]]*:[[:space:]]*"'"${repo_id}"'"'
}

create_repo_if_missing() {
  repo_id="$1"
  config_path="$2"

  if repo_exists "${repo_id}"; then
    echo "[graphdb-init] Repository ${repo_id} already exists"
    return
  fi

  echo "[graphdb-init] Creating repository ${repo_id} from ${config_path}"
  response_file="$(mktemp)"
  http_code="$(request_with_status "${response_file}" \
    -X POST "${REPOS_ENDPOINT}" \
    -H "Content-Type: multipart/form-data" \
    -F "config=@${config_path}")"

  if [ "${http_code}" -lt 200 ] || [ "${http_code}" -ge 300 ]; then
    handle_failed_graphdb_call "${http_code}" "${response_file}" "Repository creation for ${repo_id}"
  fi

  rm -f "${response_file}"
}

repo_triple_count() {
  repo_id="$1"
  query='SELECT (COUNT(*) AS ?c) WHERE { ?s ?p ?o }'

  response_file="$(mktemp)"
  http_code="$(request_with_status "${response_file}" \
    -G "${GRAPHDB_URL}/repositories/${repo_id}" \
    --data-urlencode "query=${query}" \
    -H "Accept: application/sparql-results+json")"

  if [ "${http_code}" -lt 200 ] || [ "${http_code}" -ge 300 ]; then
    handle_failed_graphdb_call "${http_code}" "${response_file}" "Triple count query for ${repo_id}"
  fi

  count="$(
    tr -d '\n' <"${response_file}" \
      | sed -n 's/.*"value"[[:space:]]*:[[:space:]]*"\([0-9][0-9]*\)".*/\1/p' \
      | head -n 1
  )"

  rm -f "${response_file}"

  if [ -z "${count}" ]; then
    count="0"
  fi

  echo "${count}"
}

import_ontology_if_empty() {
  repo_id="$1"
  ontology_path="$2"

  count="$(repo_triple_count "${repo_id}")"
 
  if [ "${count}" -gt 1000 ]; then
    echo "[graphdb-init] Repository ${repo_id} already has ${count} triples; skipping import"
    return
  fi
  if [ "${count}" != "0" ]; then
    echo "[graphdb-init] Repository ${repo_id} has only ${count} triples (system metadata); importing ontology"
  fi

  echo "[graphdb-init] Importing ${ontology_path} into ${repo_id}"
  response_file="$(mktemp)"
  http_code="$(request_with_status "${response_file}" \
    -X POST "${GRAPHDB_URL}/repositories/${repo_id}/statements" \
    -H "Content-Type: application/rdf+xml" \
    --data-binary "@${ontology_path}")"

  if [ "${http_code}" -lt 200 ] || [ "${http_code}" -ge 300 ]; then
    handle_failed_graphdb_call "${http_code}" "${response_file}" "Ontology import for ${repo_id}"
  fi

  rm -f "${response_file}"

  new_count="$(repo_triple_count "${repo_id}")"
  echo "[graphdb-init] Repository ${repo_id} now has ${new_count} triples"
}


create_repo_if_missing "DoidOntologyRepository" "/config/repo-config-DoidOntologyRepository.ttl"
create_repo_if_missing "HpOntologyRepository" "/config/repo-config-HpOntologyRepository.ttl"

import_ontology_if_empty "DoidOntologyRepository" "/data/doid-international.owl"
import_ontology_if_empty "HpOntologyRepository" "/data/hp.owl"

echo "[graphdb-init] Initialization completed"