#!/usr/bin/env bash
#
# load_synpuf.sh -- stage CMS DE-SynPUF (OMOP CDM v5.3) + the OMOP vocabulary
# into a BigQuery dataset, for the AMIA RWD workshop.
#
# Two phases, kept separate on purpose (see staging/README.md):
#
#   upload : copy local CSVs -> $WORKSPACE_BUCKET (GCS). Slow (~5 GB), done ONCE.
#            The bucket is PERSISTENT, so this survives VM/session restarts.
#   load   : create the dataset + `bq load` each table FROM GCS. Fast, idempotent
#            (`--replace`), safe to re-run every session -- which is what you do
#            if the BigQuery dataset is gone after a fresh workspace/session.
#
# Usage:
#   ./load_synpuf.sh upload      # phase 1: local CSVs -> GCS  (run once)
#   ./load_synpuf.sh load        # phase 2: GCS -> BigQuery    (re-runnable)
#   ./load_synpuf.sh all         # both
#   ./load_synpuf.sh verify      # print row counts per table
#
# Everything below is overridable from the environment, e.g.
#   BQ_DATASET=omop CLINICAL_SKIP=1 ./load_synpuf.sh load
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# BigQuery target. On the Verily/AoU workbench GOOGLE_CLOUD_PROJECT and
# WORKSPACE_BUCKET are exported into the notebook/VM environment for you.
BQ_PROJECT=${BQ_PROJECT:-${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT or BQ_PROJECT}}
BQ_DATASET=${BQ_DATASET:-synpuf}
BQ_LOCATION=${BQ_LOCATION:-US}

# Where the local CSVs live (upload phase reads these).
LOCAL_CLINICAL_DIR=${LOCAL_CLINICAL_DIR:-$HOME/workspace/resources/cmsdesynpuf100k}
LOCAL_VOCAB_DIR=${LOCAL_VOCAB_DIR:-$HOME/workspace/resources/cmsdesynpuf100k/vocab}

# Persistent GCS staging prefixes (load phase reads these).
GCS_CLINICAL=${GCS_CLINICAL:-${WORKSPACE_BUCKET:?set WORKSPACE_BUCKET or GCS_CLINICAL}/synpuf/clinical}
GCS_VOCAB=${GCS_VOCAB:-${WORKSPACE_BUCKET:?set WORKSPACE_BUCKET or GCS_VOCAB}/synpuf/vocab}

# CSV format knobs. Clinical (SynPUF) and vocabulary (Athena) differ:
#   - SynPUF clinical CSVs: comma-delimited. Header row? Depends on the export.
#     VERIFY with `head -2 condition_occurrence.csv`. If the first line is column
#     NAMES, set CLINICAL_SKIP=1. Default 0 assumes headerless (OHDSI ETL-CMS
#     output is headerless); a wrong 0 fails LOUDLY (header row won't parse as
#     INTEGER), a wrong 1 silently drops one data row -- so 0 is the safe default.
#   - Athena vocabulary CSVs: TAB-delimited, WITH a header row. Hence the
#     different defaults below.
CLINICAL_DELIM=${CLINICAL_DELIM:-,}
CLINICAL_SKIP=${CLINICAL_SKIP:-0}
VOCAB_DELIM=${VOCAB_DELIM:-$'\t'}
VOCAB_SKIP=${VOCAB_SKIP:-1}

# max_bad_records=0 => strict: any type mismatch aborts the load and shows you
# the offending row. That is what surfaces a wrong CLINICAL_SKIP or an
# unexpected date format, so keep it 0 while bringing the data up.
MAX_BAD=${MAX_BAD:-0}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_DIR="$HERE/schemas"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

upload() {  # upload <local_dir> <gcs_prefix>
  local src="$1" dst="$2"
  if [ ! -d "$src" ] || ! ls "$src"/*.csv >/dev/null 2>&1; then
    log "SKIP upload: no *.csv in $src"; return 0
  fi
  log "Uploading $src/*.csv -> $dst/  (this is the slow, one-time step)"
  gsutil -m cp "$src"/*.csv "$dst/"
}

ensure_dataset() {
  if ! bq --project_id="$BQ_PROJECT" show --dataset "$BQ_DATASET" >/dev/null 2>&1; then
    log "Creating dataset $BQ_PROJECT:$BQ_DATASET ($BQ_LOCATION)"
    bq --project_id="$BQ_PROJECT" --location="$BQ_LOCATION" mk --dataset "$BQ_DATASET"
  fi
}

# load_group <gcs_prefix> <delimiter> <skip_leading_rows>
# Loads every gs://.../<name>.csv for which schemas/<name>.json exists.
load_group() {
  local prefix="$1" delim="$2" skip="$3"
  local uri table schema
  # List CSVs actually present under the prefix (robust to session-ephemeral
  # local disk -- we drive off GCS, the persistent copy).
  local uris
  if ! uris=$(gsutil ls "$prefix"/*.csv 2>/dev/null); then
    log "SKIP load: no *.csv under $prefix"; return 0
  fi
  while IFS= read -r uri; do
    [ -z "$uri" ] && continue
    table=$(basename "$uri" .csv | tr '[:upper:]' '[:lower:]')  # CONCEPT.csv -> concept
    schema="$SCHEMA_DIR/$table.json"
    if [ ! -f "$schema" ]; then
      log "WARN: no schema for '$table' ($uri) -- skipping"; continue
    fi
    log "Loading $table  <-  $uri"
    bq --project_id="$BQ_PROJECT" --location="$BQ_LOCATION" load \
      --source_format=CSV \
      --field_delimiter="$delim" \
      --skip_leading_rows="$skip" \
      --allow_quoted_newlines \
      --max_bad_records="$MAX_BAD" \
      --replace \
      "$BQ_DATASET.$table" "$uri" "$schema"
  done <<< "$uris"
}

verify() {
  log "Row counts in $BQ_PROJECT:$BQ_DATASET"
  bq --project_id="$BQ_PROJECT" --location="$BQ_LOCATION" query --use_legacy_sql=false --format=pretty "
    SELECT table_id AS table, row_count
    FROM \`$BQ_DATASET.__TABLES__\`
    ORDER BY table_id
  "
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-help}" in
  upload)
    upload "$LOCAL_CLINICAL_DIR" "$GCS_CLINICAL"
    upload "$LOCAL_VOCAB_DIR"    "$GCS_VOCAB"
    ;;
  load)
    ensure_dataset
    load_group "$GCS_CLINICAL" "$CLINICAL_DELIM" "$CLINICAL_SKIP"
    load_group "$GCS_VOCAB"    "$VOCAB_DELIM"    "$VOCAB_SKIP"
    ;;
  all)
    upload "$LOCAL_CLINICAL_DIR" "$GCS_CLINICAL"
    upload "$LOCAL_VOCAB_DIR"    "$GCS_VOCAB"
    ensure_dataset
    load_group "$GCS_CLINICAL" "$CLINICAL_DELIM" "$CLINICAL_SKIP"
    load_group "$GCS_VOCAB"    "$VOCAB_DELIM"    "$VOCAB_SKIP"
    ;;
  verify)
    verify
    ;;
  *)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -40
    ;;
esac
