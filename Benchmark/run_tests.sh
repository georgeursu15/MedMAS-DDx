#!/usr/bin/env bash
# ── MedMAS_DDx batch test runner — DGX / NVIDIA L40S ────────────────────────
#
# Builds the Docker image (if needed) and runs eval_runner inside the
# app container.  Results land in ./eval_results/ and ./logs/ on the host.
#
# USAGE
#   ./run_tests.sh [OPTIONS] [-- EVAL_RUNNER_ARGS...]
#
# OPTIONS
#   --build          Force-rebuild the app image before running
#   --pull-model     Pull the Ollama model (only needed once, or after change)
#   --up             Start GraphDB + Ollama in the background (needed first time)
#   --down           Stop and remove all DGX containers
#   --config FILE    Run a batch of tests defined in a JSON config file
#                    (replicates the frontend "Add to batch → Batch(N)" flow)
#   --help           Show this message
#
# EVAL_RUNNER_ARGS  (passed directly to python -m MedMAS_DDx.evaluation.eval_runner)
#   --variant 100|80|50   Run only one symptom-completeness variant
#   --prognosis NAME      Filter by disease name (substring, case-insensitive)
#   --limit N             Cap number of (case, variant) pairs
#   --batch-id ID         Group multiple variant runs into one folder
#   --retry-failed FILE   Re-run only the failed cases from a previous JSON
#
# EXAMPLES
#   # First-time setup on the DGX:
#   ./run_tests.sh --up --pull-model
#
#   # Quick smoke test (5 cases, 100% variant):
#   ./run_tests.sh -- --variant 100 --limit 5
#
#   # Full run — all variants (sequential, same batch folder):
#   ./run_tests.sh --full
#
#   # Batch run from a config file (same as frontend multi-run):
#   cp tests.example.json tests.json   # edit to your liking
#   ./run_tests.sh --config tests.json
#
#   # Re-run only failures from a previous batch:
#   ./run_tests.sh -- --retry-failed eval_results/batch_YYYYMMDD_HHMMSS/diabetes_100pct.txt \
#                      --batch-id batch_YYYYMMDD_HHMMSS
#
#   # Tear everything down:
#   ./run_tests.sh --down

set -euo pipefail

# Always run from the project root (one level above this script's directory)
cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.dgx.yml"
APP_SERVICE="app"
COMPOSE="docker compose -f ${COMPOSE_FILE}"

# ── Helpers ───────────────────────────────────────────────────────────────────

usage() {
  sed -n '/^# USAGE/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \{0,2\}//'
  exit 0
}

log() { echo "[run_tests] $*"; }

ensure_dirs() {
  mkdir -p eval_results logs
}

build_image() {
  log "Building app image ..."
  ${COMPOSE} build ${APP_SERVICE}
}

pull_model() {
  log "Pulling Ollama model (this may take several minutes the first time) ..."
  ${COMPOSE} run --rm ollama-pull
  log "Model pull complete."
}

start_services() {
  log "Starting GraphDB + Ollama in the background ..."
  ${COMPOSE} up -d graphdb ollama
  log "Waiting for GraphDB init ..."
  ${COMPOSE} run --rm graphdb-init || true   # idempotent; exits 0 if repos exist
  log "Infrastructure is up."
}

stop_services() {
  log "Stopping all DGX containers ..."
  ${COMPOSE} down
}

run_eval() {
  local args=("$@")
  ensure_dirs
  log "Running eval_runner: ${args[*]:-<default: all variants>}"
  ${COMPOSE} run --rm ${APP_SERVICE} test "${args[@]}"
  log "Done. Results are in ./eval_results/"
}

run_full() {
  # Generate a shared batch ID so all variants land in the same folder
  local batch_id
  batch_id="batch_$(date +%Y%m%d_%H%M%S)"
  log "Full run — batch ID: ${batch_id}"
  ensure_dirs
  for variant in 100 80 50; do
    log "  Running variant ${variant}% ..."
    ${COMPOSE} run --rm ${APP_SERVICE} test \
      --variant "${variant}" \
      --batch-id "${batch_id}"
  done
  log "Full run complete. Results in ./eval_results/${batch_id}/"
}

# ── Argument parsing ──────────────────────────────────────────────────────────

DO_BUILD=0
DO_PULL=0
DO_UP=0
DO_DOWN=0
DO_FULL=0
CONFIG_FILE=""
EVAL_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --build)       DO_BUILD=1;        shift ;;
    --pull-model)  DO_PULL=1;         shift ;;
    --up)          DO_UP=1;           shift ;;
    --down)        DO_DOWN=1;         shift ;;
    --full)        DO_FULL=1;         shift ;;
    --config)      CONFIG_FILE="$2";  shift 2 ;;
    --help|-h)     usage ;;
    --)            shift; EVAL_ARGS=("$@"); break ;;
    *)             EVAL_ARGS+=("$1"); shift ;;
  esac
done

# ── Actions ───────────────────────────────────────────────────────────────────

[[ ${DO_DOWN} -eq 1 ]]  && stop_services && exit 0
[[ ${DO_BUILD} -eq 1 ]] && build_image
[[ ${DO_UP} -eq 1 ]]    && start_services
[[ ${DO_PULL} -eq 1 ]]  && pull_model

if [[ -n "${CONFIG_FILE}" ]]; then
  ensure_dirs
  # Resolve to absolute path so Docker bind-mount can see it
  ABS_CONFIG="$(cd "$(dirname "${CONFIG_FILE}")" && pwd)/$(basename "${CONFIG_FILE}")"
  log "Running batch from config: ${ABS_CONFIG}  extra args: ${EVAL_ARGS[*]:-<none>}"
  # Any args after `--` (or unrecognised flags) are forwarded to batch_runner,
  # so callers can enable e.g. --patient-llm without editing this script:
  #   ./run_tests.sh --config tests.json -- --patient-llm
  ${COMPOSE} run --rm \
    -v "${ABS_CONFIG}:/app/batch_config.json:ro" \
    ${APP_SERVICE} \
    test --config /app/batch_config.json "${EVAL_ARGS[@]}"
elif [[ ${DO_FULL} -eq 1 ]]; then
  run_full
elif [[ ${#EVAL_ARGS[@]} -gt 0 ]]; then
  run_eval "${EVAL_ARGS[@]}"
elif [[ ${DO_UP} -eq 0 && ${DO_PULL} -eq 0 && ${DO_BUILD} -eq 0 ]]; then
  usage
fi
