#!/usr/bin/env bash
# Load an Athena vocabulary bundle into the stack and register it with WebAPI.
#
# Usage (from anywhere):  atlas/load-vocab.sh [/path/to/athena/bundle]
# The path defaults to VOCAB_DIR from the env file. ENV_FILE selects the env
# file (default .env, relative to this directory).
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "ERROR: $*" >&2; exit 1; }

ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found (copy .env.example to .env)"
set -a; . "./$ENV_FILE"; set +a
[[ $# -ge 1 ]] && VOCAB_DIR="$1"
export VOCAB_DIR

WEBAPI="http://localhost:${WEBAPI_PORT:-8080}/WebAPI"
REQUIRED=(CONCEPT CONCEPT_ANCESTOR CONCEPT_CLASS CONCEPT_RELATIONSHIP
          CONCEPT_SYNONYM DOMAIN DRUG_STRENGTH RELATIONSHIP VOCABULARY)

dc() { docker compose --env-file "$ENV_FILE" "$@"; }

step() { # label cmd...
  local label=$1 start=$SECONDS; shift
  echo "==> $label"
  "$@"
  echo "    done in $((SECONDS - start))s"
}

wait_webapi() {
  for _ in $(seq 1 60); do
    curl -fs "$WEBAPI/info" >/dev/null && return 0
    sleep 5
  done
  die "WebAPI did not become ready at $WEBAPI"
}

solr_running() { dc ps --status running --services | grep -qx solr; }

# Validate before touching anything, so a bad bundle leaves the current vocab intact.
[[ -n "${VOCAB_DIR:-}" ]] || die "no vocabulary directory: pass one or set VOCAB_DIR in $ENV_FILE"
[[ -d "$VOCAB_DIR" ]] || die "vocabulary directory not found: $VOCAB_DIR"
missing=()
for t in "${REQUIRED[@]}"; do
  [[ -f "$VOCAB_DIR/$t.csv" ]] || missing+=("$t.csv")
done
(( ${#missing[@]} == 0 )) || die "$VOCAB_DIR is missing: ${missing[*]}"

step "Starting stack" dc up -d --wait
step "Loading vocabulary from $VOCAB_DIR" dc --profile load run --rm vocab-loader
step "Refreshing WebAPI source cache" curl -fsS -o /dev/null "$WEBAPI/source/refresh"
echo "Vocabulary loaded. ATLAS: http://localhost:${ATLAS_PORT:-8081}/atlas/"
