from __future__ import annotations

import json
import os
from typing import Type
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class HpOntologyQueryToolInput(BaseModel):


    search_term: str = Field(
        ...,
        description=(
            "The symptom name or phrase to search for in the Human Phenotype "
            "Ontology (HP). The tool searches HP exact-synonym annotations for "
            "matches and returns the matched terms' labels, which can then be "
            "used to retry a SYMP ontology lookup."
        ),
    )


class HpOntologyQueryTool(BaseTool):
    

    name: str = "hp_ontology_query"
    description: str = (
        "Search the Human Phenotype Ontology (HP) in GraphDB for a symptom "
        "term. Use this as a FALLBACK when graphdb_ontology_query (SYMP) "
        "returns no results for a symptom.\n\n"

        "=== WHEN TO USE ===\n"
        "Call this tool ONLY after graphdb_ontology_query has failed to find "
        "a SYMP match for a symptom (after your initial retries with "
        "simplification/synonyms). This tool searches HP exact-synonym "
        "annotations for your search term.\n\n"

        "=== HOW TO CALL ===\n"
        "Send exactly: {\"search_term\": \"<symptom text>\"}\n"
        "Example: {\"search_term\": \"burning micturition\"}\n"
        "Example: {\"search_term\": \"spinning movements\"}\n"
        "Example: {\"search_term\": \"passage of gases\"}\n\n"

        "=== WHAT IT RETURNS ===\n"
        "A JSON object with:\n"
        "- hp_matches: list of {hp_uri, hp_label, match_type} objects\n"
        "  match_type is always 'synonym' — the term matched via an "
        "exact-synonym annotation on the HP term; hp_label is that term's "
        "rdfs:label (returned for display and reuse, not itself a match "
        "criterion)\n"
        "- suggested_symp_search_terms: list of HP label strings you should "
        "use to retry a SYMP search via graphdb_ontology_query\n\n"

        "=== WORKFLOW AFTER CALLING ===\n"
        "1. Read suggested_symp_search_terms from the response.\n"
        "2. For each suggested term, call graphdb_ontology_query with a "
        "PATTERN 1 query using that term as the search text.\n"
        "3. If a SYMP match is found, use it. If not, the symptom is unmapped.\n\n"

        "=== RULES ===\n"
        "- This tool queries a DIFFERENT GraphDB repository (HP ontology).\n"
        "- Use HP results ONLY as search terms for retrying SYMP lookups.\n"
        "- Do NOT use HP URIs as SYMP URIs — they are different ontologies.\n"
        "- Do NOT skip this fallback step; it often finds synonyms that SYMP "
        "recognizes.\n"
    )
    args_schema: Type[BaseModel] = HpOntologyQueryToolInput

    _hp_repository_url: str = "http://localhost:7200/repositories/HpOntologyRepository"

    def __init__(
        self,
        timeout_seconds: int = 30,
        hp_repository_url: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._timeout_seconds = timeout_seconds
        self._hp_repository_url = (
            hp_repository_url
            or os.environ.get("HP_GRAPHDB_REPOSITORY_URL")
            or "http://localhost:7200/repositories/HpOntologyRepository"
        )

    def _build_hp_search_query(self, search_term: str) -> str:
        escaped_term = search_term.strip().lower().replace('"', '\\"')

        return f"""
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX obo:  <http://purl.obolibrary.org/obo/>
PREFIX oboInOwl: <http://www.geneontology.org/formats/oboInOwl#>
PREFIX IAO: <http://purl.obolibrary.org/obo/IAO_>

SELECT DISTINCT ?term ?termLabel ?matchType
WHERE {{
  {{
    ?term oboInOwl:hasExactSynonym ?synonym .
    ?term rdfs:label ?termLabel .
    FILTER(CONTAINS(LCASE(STR(?synonym)), "{escaped_term}"))
    BIND("synonym" AS ?matchType)
  }}
  FILTER(STRSTARTS(STR(?term), "http://purl.obolibrary.org/obo/HP_"))
}}
LIMIT 20
"""

    def _execute_sparql(self, query: str) -> dict | str:
        """Execute a SPARQL query against the HP repository."""
        payload = urlencode({"query": query.strip()}).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/sparql-results+json, application/json;q=0.9, */*;q=0.8",
        }
        request = Request(
            self._hp_repository_url,
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return (
                f"HP ontology GraphDB request failed with HTTP {exc.code}. "
                f"Endpoint Response: {body}"
            )
        except URLError as exc:
            return (
                "Could not reach HP ontology GraphDB endpoint. "
                f"Endpoint Error: {exc.reason}"
            )
        except TimeoutError:
            return f"HP ontology request timed out after {self._timeout_seconds}s."
        except Exception as exc:
            return f"Unexpected error querying HP ontology: {exc}"

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return f"HP ontology query returned non-JSON response: {raw[:500]}"

    def _run(self, search_term: str) -> str:
        clean_term = search_term.strip()
        if not clean_term:
            return json.dumps({
                "error": "search_term is empty",
                "hp_matches": [],
                "suggested_symp_search_terms": [],
            })

        query = self._build_hp_search_query(clean_term)
        result = self._execute_sparql(query)

        if isinstance(result, str):
            return json.dumps({
                "error": result,
                "hp_matches": [],
                "suggested_symp_search_terms": [],
                "executed_query": query.strip(),
            })

        bindings = result.get("results", {}).get("bindings", [])

        hp_matches: list[dict[str, str]] = []
        seen_labels: set[str] = set()
        suggested_terms: list[str] = []

        for row in bindings:
            term_uri = row.get("term", {}).get("value", "")
            term_label = row.get("termLabel", {}).get("value", "")
            match_type = row.get("matchType", {}).get("value", "unknown")

            if not term_label:
                continue

            hp_matches.append({
                "hp_uri": term_uri,
                "hp_label": term_label,
                "match_type": match_type,
            })

            label_lower = term_label.strip().lower()
            if label_lower not in seen_labels:
                seen_labels.add(label_lower)
                suggested_terms.append(term_label.strip())

        output = {
            "search_term": clean_term,
            "executed_query": query.strip(),
            "result_count": len(bindings),
            "hp_matches": hp_matches,
            "suggested_symp_search_terms": suggested_terms,
        }

        return json.dumps(output, ensure_ascii=True)