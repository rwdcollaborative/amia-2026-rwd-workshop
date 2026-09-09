#!/usr/bin/env python3
"""Generate BigQuery JSON load schemas for the OMOP CDM v5.3 tables.

We parse the *canonical* OHDSI DDL (reference/OMOPCDM_bigquery_5.3_ddl.sql,
pulled verbatim from OHDSI/CommonDataModel @ v5.3.1) rather than hand-typing 39
tables, so the column names and types cannot drift from the spec.

Two deliberate departures from the DDL, both to make `bq load` robust for a
teaching corpus rather than a production warehouse:

1. Every field is emitted as NULLABLE, even columns the DDL marks `not null`.
   A REQUIRED field makes `bq load` reject the *entire file* on the first row
   with a null in that column. Synthetic exports routinely have nulls where the
   spec says otherwise; for a workshop, loading the data beats enforcing the
   constraint. (Real referential integrity is out of scope here.)

2. The vocabulary date columns (valid_start_date / valid_end_date, and the
   source_to_concept_map / drug_strength equivalents) are emitted as STRING,
   not DATE. The OHDSI Athena vocabulary CSVs encode these as `YYYYMMDD`
   (e.g. 20991231), which BigQuery will NOT parse as a DATE on load -- the load
   fails. Loading them as STRING always succeeds; a downstream query can
   PARSE_DATE('%Y%m%d', valid_start_date) if it ever needs the real date.
   The clinical event dates (condition_start_date, etc.) are left as DATE/
   DATETIME on the assumption the SynPUF export uses ISO `YYYY-MM-DD` -- verify
   with `head -2` of a clinical CSV before loading (see staging/README.md).

Run:  python3 generate_schemas.py
Out:  schemas/<table>.json  (one BigQuery schema file per CDM table)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DDL = HERE / "reference" / "OMOPCDM_bigquery_5.3_ddl.sql"
OUT = HERE / "schemas"

# DDL type -> BigQuery JSON schema type
TYPE_MAP = {
    "INT64": "INTEGER",
    "FLOAT64": "FLOAT",
    "STRING": "STRING",
    "DATE": "DATE",
    "DATETIME": "DATETIME",
}

# Columns to force to STRING because Athena vocabulary CSVs store them as
# YYYYMMDD, which BigQuery cannot load into a DATE column. Keyed by (table,
# column); see module docstring point (2).
FORCE_STRING = {
    ("concept", "valid_start_date"),
    ("concept", "valid_end_date"),
    ("concept_relationship", "valid_start_date"),
    ("concept_relationship", "valid_end_date"),
    ("drug_strength", "valid_start_date"),
    ("drug_strength", "valid_end_date"),
    ("source_to_concept_map", "valid_start_date"),
    ("source_to_concept_map", "valid_end_date"),
}

# Matches:  create table NAME ( ... ) ;   (case-insensitive, DOTALL body)
TABLE_RE = re.compile(
    r"create\s+table\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def parse_columns(body: str) -> list[tuple[str, str]]:
    """Return [(col_name, ddl_type), ...] from a CREATE TABLE body."""
    cols: list[tuple[str, str]] = []
    for raw in body.split(","):
        # Drop DDL comments (e.g. the --HINT lines) and blank fragments.
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        # A column line is:  <name> <TYPE> [not null] ...
        # note_nlp has a quoted "offset" column -> strip the quotes.
        tokens = line.replace('"', "").split()
        if len(tokens) < 2:
            continue
        name, ddl_type = tokens[0].lower(), tokens[1].upper()
        if ddl_type not in TYPE_MAP:
            continue  # not a column line we understand
        cols.append((name, ddl_type))
    return cols


def main() -> None:
    text = DDL.read_text()
    OUT.mkdir(exist_ok=True)
    written = 0
    for match in TABLE_RE.finditer(text):
        table = match.group(1).lower()
        fields = []
        for name, ddl_type in parse_columns(match.group(2)):
            bq_type = TYPE_MAP[ddl_type]
            if (table, name) in FORCE_STRING:
                bq_type = "STRING"
            # Everything NULLABLE on purpose -- see docstring point (1).
            fields.append({"name": name, "type": bq_type, "mode": "NULLABLE"})
        if not fields:
            continue
        (OUT / f"{table}.json").write_text(json.dumps(fields, indent=2) + "\n")
        written += 1
    print(f"Wrote {written} schema files to {OUT}")


if __name__ == "__main__":
    main()
