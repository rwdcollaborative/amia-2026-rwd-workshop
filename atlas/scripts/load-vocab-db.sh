#!/usr/bin/env bash
# Runs inside the vocab-loader container (postgres image, for psql).
# Recreates the vocab schema from the Athena CSVs mounted at /vocab and
# registers it with WebAPI. Bundle validation happens on the host first.
set -euo pipefail

TABLES=(concept vocabulary domain concept_class concept_relationship
        relationship concept_synonym concept_ancestor drug_strength)

psql_run() { psql -v ON_ERROR_STOP=1 -q "$@"; }

echo "  creating vocab schema"
psql_run -f /sql/vocab_ddl.sql

for t in "${TABLES[@]}"; do
  start=$SECONDS
  # Athena files are tab-separated with literal quote characters in text, so
  # disable CSV quoting by setting the quote char to backspace.
  psql_run -c "\\copy vocab.$t FROM '/vocab/${t^^}.csv' WITH (FORMAT csv, DELIMITER E'\\t', QUOTE E'\\b', HEADER true)"
  echo "  loaded $t: $(psql -tAc "select count(*) from vocab.$t") rows ($((SECONDS - start))s)"
done

echo "  adding keys and indexes"
psql_run -f /sql/vocab_indexes.sql

echo "  registering VOCAB source with WebAPI"
psql_run -v jdbc_url="jdbc:postgresql://db:5432/$PGDATABASE" \
         -v db_user="$PGUSER" -v db_password="$PGPASSWORD" \
         -f /sql/register_source.sql
