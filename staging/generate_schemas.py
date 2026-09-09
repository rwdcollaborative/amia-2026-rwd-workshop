#!/usr/bin/env python3
"""Generate BigQuery JSON load schemas for the OMOP CDM tables.

Two modes:

  DDL mode (default) -- parse the canonical OHDSI DDL
    (reference/OMOPCDM_bigquery_5.3_ddl.sql, from OHDSI/CommonDataModel @
    v5.3.1) and emit one schema per table in the DDL's column order. Use this
    when your CSVs match CDM v5.3.1 exactly.

      python3 generate_schemas.py

  HEADER mode -- read the *actual* CSV header of each file and emit a schema in
    THAT column order, looking up each column's type by name. Use this when your
    CSVs are a different CDM revision than the DDL, because `bq load` maps CSV
    columns BY POSITION, not by header name -- so the schema's column order must
    match the file's, or every column lands in the wrong field.

      python3 generate_schemas.py --from-csv ~/workspace/resources/cmsdesynpuf100k

    The CMS DE-SynPUF export is CDM 5.2-shaped (no visit_detail_id, no
    *_datetime, no condition_status_* columns), so its clinical files do NOT
    line up with the v5.3.1 DDL -- header mode is what makes them load. Requires
    a header row in each CSV (load with CLINICAL_SKIP=1).

Two deliberate departures from the DDL, both to make `bq load` robust for a
teaching corpus rather than a production warehouse (they apply in both modes):

1. Every field is emitted as NULLABLE, even columns the DDL marks `not null`.
   A REQUIRED field makes `bq load` reject the *entire file* on the first row
   with a null in that column.

2. The vocabulary date columns (valid_start_date / valid_end_date on concept,
   concept_relationship, drug_strength, source_to_concept_map) are emitted as
   STRING, not DATE: the OHDSI Athena CSVs encode them as `YYYYMMDD`
   (e.g. 20991231), which BigQuery will NOT parse into a DATE on load. A query
   can PARSE_DATE('%Y%m%d', valid_start_date) later if it needs the real date.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
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

# Columns forced to STRING because Athena vocabulary CSVs store them as YYYYMMDD,
# which BigQuery cannot load into a DATE column. Keyed by (table, column).
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

# Vocabulary tables. Header mode SKIPS these: they come from Athena and already
# match the DDL, so their committed DDL-mode schemas load correctly -- we don't
# want a stray clinical-dir copy (e.g. drug_strength.csv) to overwrite them.
VOCAB_TABLES = {
    "concept", "concept_ancestor", "concept_class", "concept_cpt4",
    "concept_metadata", "concept_relationship", "concept_relationship_metadata",
    "concept_synonym", "domain", "drug_strength", "pack_content", "relationship",
    "source_to_concept_map", "vocabulary",
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
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        # note_nlp has a quoted "offset" column -> strip the quotes.
        tokens = line.replace('"', "").split()
        if len(tokens) < 2:
            continue
        name, ddl_type = tokens[0].lower(), tokens[1].upper()
        if ddl_type not in TYPE_MAP:
            continue
        cols.append((name, ddl_type))
    return cols


def parse_ddl() -> dict[str, list[tuple[str, str]]]:
    """table -> [(col, ddl_type), ...] for every table in the DDL."""
    text = DDL.read_text()
    tables: dict[str, list[tuple[str, str]]] = {}
    for match in TABLE_RE.finditer(text):
        cols = parse_columns(match.group(2))
        if cols:
            tables[match.group(1).lower()] = cols
    return tables


def name_type_map(tables: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """column name -> BigQuery type, unioned across all DDL tables.

    OMOP column names carry a consistent type wherever they appear, so a plain
    union is safe; the first definition wins on the rare clash.
    """
    m: dict[str, str] = {}
    for cols in tables.values():
        for name, ddl_type in cols:
            m.setdefault(name, TYPE_MAP[ddl_type])
    return m


def field(table: str, name: str, bq_type: str) -> dict:
    if (table, name) in FORCE_STRING:
        bq_type = "STRING"
    return {"name": name, "type": bq_type, "mode": "NULLABLE"}


def write_schema(table: str, fields: list[dict]) -> None:
    (OUT / f"{table}.json").write_text(json.dumps(fields, indent=2) + "\n")


def gen_from_ddl(tables: dict[str, list[tuple[str, str]]]) -> int:
    OUT.mkdir(exist_ok=True)
    written = 0
    for table, cols in tables.items():
        fields = [field(table, name, TYPE_MAP[t]) for name, t in cols]
        if fields:
            write_schema(table, fields)
            written += 1
    return written


def read_header(csv: Path) -> tuple[list[str], str]:
    """Return (lowercased column names, delimiter) from a CSV's first line."""
    with csv.open(encoding="utf-8-sig", newline="") as fh:
        first = fh.readline().rstrip("\r\n")
    delim = "\t" if "\t" in first else ","
    cols = [c.strip().strip('"').lower() for c in first.split(delim)]
    return cols, delim


def gen_from_csv(dirs: list[Path], types: dict[str, str]) -> int:
    OUT.mkdir(exist_ok=True)
    written = 0
    for d in dirs:
        if not d.is_dir():
            print(f"  skip: not a directory: {d}", file=sys.stderr)
            continue
        for csv in sorted(d.glob("*.csv")):
            table = csv.stem.lower()
            if table in VOCAB_TABLES:
                print(f"  skip (vocabulary table, keeping DDL schema): {table}",
                      file=sys.stderr)
                continue
            cols, _ = read_header(csv)
            if not cols or cols == [""]:
                print(f"  skip (no header): {csv.name}", file=sys.stderr)
                continue
            unknown = [c for c in cols if c not in types]
            fields = [field(table, c, types.get(c, "STRING")) for c in cols]
            write_schema(table, fields)
            written += 1
            note = f"  ({len(unknown)} col(s) -> STRING: {', '.join(unknown)})" if unknown else ""
            print(f"  {table}: {len(fields)} cols from header{note}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-csv", metavar="DIR", action="append", default=[],
                    help="generate schemas from the CSV headers in DIR (column "
                         "ORDER taken from each file's header, types looked up "
                         "by name from the DDL). Repeatable. Vocabulary tables "
                         "are skipped. Without this flag, schemas are generated "
                         "from the DDL in DDL column order.")
    args = ap.parse_args()

    tables = parse_ddl()
    if args.from_csv:
        types = name_type_map(tables)
        written = gen_from_csv([Path(p).expanduser() for p in args.from_csv], types)
        print(f"Wrote {written} schema files to {OUT} (from CSV headers)")
    else:
        written = gen_from_ddl(tables)
        print(f"Wrote {written} schema files to {OUT} (from DDL)")


if __name__ == "__main__":
    main()
