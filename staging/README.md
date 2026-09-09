# Staging DE-SynPUF into BigQuery

Load the synthetic **CMS DE-SynPUF** data (OMOP CDM v5.3) plus the **OMOP
vocabulary** into a BigQuery dataset, so workshop participants can query OMOP
tables with plain BigQuery SQL — the same shape of workflow as the real All of
Us CDR, without touching real EHR data.

## What's here

| File | Purpose |
|------|---------|
| `load_synpuf.sh` | Two-phase loader: `upload` (local CSVs → GCS), `load` (GCS → BigQuery). |
| `generate_schemas.py` | Regenerates `schemas/` from the canonical OHDSI DDL. |
| `schemas/*.json` | One BigQuery load schema per CDM v5.3 table (39 tables). |
| `reference/OMOPCDM_bigquery_5.3_ddl.sql` | Canonical DDL, from OHDSI/CommonDataModel @ v5.3.1. |

## Why two phases

- **`upload`** copies ~5 GB of CSVs into `$WORKSPACE_BUCKET` (GCS). The bucket is
  **persistent** — it survives VM shutdowns and session restarts — so you pay
  this cost **once**. On the Verily Workbench the `resources` bucket is already
  gcsfuse-**mounted** at `~/workspace/resources`, so the CSVs are *already in
  GCS* — the script detects this (via `findmnt`) and **skips `upload`
  automatically**. You only need `upload` if your CSVs sit on a plain local disk.
- **`load`** creates the dataset and `bq load`s each table **from GCS**. It is
  fast and **idempotent** (`--replace`), so it is safe to re-run any time a
  BigQuery dataset needs (re)building — e.g. in each participant's workspace.

The load phase reads from `GCS_CLINICAL` / `GCS_VOCAB`, which default, in order,
to (1) the `gs://` URI backing the mounted local dir, else (2) a staging prefix
under `$WORKSPACE_BUCKET`. Export either variable to override.

The dataset itself is durable: create it once as a Verily Workbench BigQuery
resource (UI, or `wb resource create bq-dataset`) or let the script `bq mk` it.
The script uses whichever already exists and only creates it when missing.

## Prerequisites

1. **The SynPUF CSVs**, already downloaded to (default)
   `~/workspace/resources/cmsdesynpuf100k/`.
2. **The OMOP vocabulary CSVs** — download from [Athena](https://athena.ohdsi.org)
   (at minimum `CONCEPT`, `CONCEPT_ANCESTOR`, `CONCEPT_RELATIONSHIP`,
   `VOCABULARY`) into `~/workspace/resources/cmsdesynpuf100k/vocab/`. **Nothing works
   without `concept`** — every human-readable name comes from a join to it.
   Athena files are **tab-delimited with a header row**; the script already
   handles that (`VOCAB_DELIM=$'\t'`, `VOCAB_SKIP=1`).
3. `gsutil`, `bq`, and `findmnt` on PATH (all present on the Workbench VM), and
   `GOOGLE_CLOUD_PROJECT` set (exported for you on the Workbench; the dataset is
   created here). `WORKSPACE_BUCKET` is **only** needed if your CSVs are *not*
   already on a mounted bucket — when they are (the Workbench default), the
   script finds the `gs://` location from the mount and no `WORKSPACE_BUCKET` or
   `upload` step is required.

## ⚠️ One thing to verify before loading: do the clinical CSVs have a header?

```bash
head -2 ~/workspace/resources/cmsdesynpuf100k/condition_occurrence.csv
```

- If the first line is **column names** → the export has a header:
  run with `CLINICAL_SKIP=1`.
- If the first line is already **data** (starts with numbers) → headerless
  (the default, `CLINICAL_SKIP=0`).

Also glance at the date columns in that output. The schemas expect ISO
`YYYY-MM-DD` for clinical dates. If they're some other format the load will fail
loudly (that's `MAX_BAD=0` doing its job) — tell me the format and we adjust.

## Run it

On the Workbench (CSVs already on the mounted `resources` bucket) it's one step —
the dataset is created for you and `upload` is skipped automatically:

```bash
cd staging

# Builds the BigQuery dataset from GCS. Creates the dataset if missing.
#   add CLINICAL_SKIP=1 if head -2 showed a header row
./load_synpuf.sh load

# Sanity check: row counts per table
./load_synpuf.sh verify
```

If instead your CSVs are on a plain local disk, run the one-time `upload` first
(needs `WORKSPACE_BUCKET`), then `load`:

```bash
./load_synpuf.sh upload    # local CSVs -> $WORKSPACE_BUCKET (once)
./load_synpuf.sh load
```

Override any default inline, e.g. a different dataset name or project:

```bash
BQ_DATASET=omop BQ_PROJECT=my-proj ./load_synpuf.sh load
```

## Then query it

```sql
SELECT co.condition_concept_id, c.concept_name, COUNT(*) AS n
FROM `synpuf.condition_occurrence` co
JOIN `synpuf.concept` c ON c.concept_id = co.condition_concept_id
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 20;
```

## Notes on the schemas (deliberate choices)

- **All columns are `NULLABLE`**, even those the DDL marks `not null`. A
  `REQUIRED` field makes `bq load` reject the whole file on the first null;
  loading a teaching corpus beats enforcing constraints.
- **Vocabulary `valid_start_date` / `valid_end_date` are `STRING`**, because
  Athena stores them as `YYYYMMDD`, which BigQuery cannot load into a `DATE`
  column. `PARSE_DATE('%Y%m%d', valid_start_date)` in a query if you ever need
  the real date.

Regenerate the schemas after editing the DDL or the overrides:

```bash
python3 generate_schemas.py
```
