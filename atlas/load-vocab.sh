#!/usr/bin/env bash
# Load an Athena vocabulary bundle into the stack and register it with WebAPI.
#
# Usage (from anywhere):  atlas/load-vocab.sh [/path/to/athena/bundle]
# A relative bundle path is taken relative to where you run the script. It
# defaults to VOCAB_DIR from the env file (relative paths there are relative
# to this directory). ENV_FILE selects the env file (default .env, relative
# to this directory, or absolute).
set -euo pipefail
# Resolve the bundle argument against the caller's directory before the cd.
ARG_DIR=
if [[ $# -ge 1 ]]; then
  case "$1" in /*) ARG_DIR=$1 ;; *) ARG_DIR="$PWD/$1" ;; esac
fi
cd "$(dirname "$0")"

die() { echo "ERROR: $*" >&2; exit 1; }

ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found (copy .env.example to .env)"
case "$ENV_FILE" in /*) env_path=$ENV_FILE ;; *) env_path="./$ENV_FILE" ;; esac
set -a; . "$env_path"; set +a
[[ -n "$ARG_DIR" ]] && VOCAB_DIR="$ARG_DIR"
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

build_solr_index() {
  local solr="http://localhost:${SOLR_PORT:-8983}/solr" version core status
  version=$(dc exec -T db psql -U "${POSTGRES_USER:-ohdsi}" -d "${POSTGRES_DB:-ohdsi}" -tAc \
    "select vocabulary_version from vocab.vocabulary where vocabulary_id = 'None'")
  [[ -n "$version" ]] || die "no 'None' row in vocab.vocabulary; cannot name the Solr core"
  # WebAPI looks cores up by this exact transformation of the version string.
  core="${version// /_}"
  echo "    core: $core"

  if ! curl -fsS "$solr/admin/cores?action=STATUS&core=$core&wt=json" | grep -q "\"name\":\"$core\""; then
    curl -fsS -o /dev/null "$solr/admin/cores?action=CREATE&name=$core&configSet=omop_vocab"
  fi
  curl -fsS -o /dev/null \
    "$solr/$core/dataimport?command=full-import&entity=concept&clean=true&commit=true&vocab_database_schema=vocab"
  sleep 2
  while status=$(curl -fsS "$solr/$core/dataimport?command=status&wt=json"); \
        grep -q '"status":"busy"' <<<"$status"; do
    sleep 5
  done
  grep -q 'Indexing completed' <<<"$status" || die "Solr import failed: $status"
}

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
if solr_running; then
  step "Building Solr index" build_solr_index
  # WebAPI reads the Solr core list only at startup.
  step "Restarting WebAPI" dc restart webapi
  step "Waiting for WebAPI" wait_webapi
else
  echo "==> Solr not running; skipping index (search uses SQL)"
  step "Refreshing WebAPI source cache" curl -fsS -o /dev/null "$WEBAPI/source/refresh"
fi
echo "Vocabulary loaded. ATLAS: http://localhost:${ATLAS_PORT:-8081}/atlas/"
