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
| `diagnostics.py` | Read-only survey of BigQuery access and the loaded data. |
| `augment.py` | Builds `synpuf_workshop`: SynPUF plus the synthetic records the exercises need. |
| `data/h3res3_countyfips_cw.csv` | County FIPS → H3 resolution-3 hexagon (one hexagon per county). |
| `data/h3_res3.gpkg` | GeoPackage of the H3 resolution-3 hexagon polygons, keyed by `h3_id`. |
| `data/ssa_fips_state_county2011.csv` | NBER SSA ↔ FIPS county crosswalk ([source](https://www.nber.org/research/data/ssa-federal-information-processing-series-fips-state-and-county-crosswalk)). |

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

- If the first line is **column names** → the export has a header. This is the
  case for the workshop data, and `CLINICAL_SKIP=1` is now the **default**, so
  there's nothing to set.
- If the first line is already **data** (starts with numbers) → headerless:
  run with `CLINICAL_SKIP=0` (which also disables the header-based schema regen).

Also glance at the date columns in that output. The schemas expect ISO
`YYYY-MM-DD` for clinical dates. If they're some other format the load will fail
loudly (that's `MAX_BAD=0` doing its job) — tell me the format and we adjust.

## Run it

On the Workbench (CSVs already on the mounted `resources` bucket) it's one step —
the dataset is created for you and `upload` is skipped automatically:

```bash
cd staging

# Builds the BigQuery dataset from GCS. Creates the dataset if missing, and
# rebuilds the clinical schemas from the CSV headers first, so a fresh clone
# needs no separate generate step. (Run with no args to print help + paths.)
./load_synpuf.sh load

# Sanity check: row counts per table
./load_synpuf.sh verify
```

> `CLINICAL_SKIP=1` (header row present) is the default. `load` regenerates the
> clinical schemas from the local CSV headers each run (when `CLINICAL_SKIP=1`
> and the CSVs are on hand) — see the schemas note below. `REGEN_SCHEMAS=0` loads
> against the committed schemas as-is; `CLINICAL_SKIP=0` is for headerless CSVs.

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

- **Clinical schemas are generated from the CSV *headers*, not the DDL.** The
  CMS DE-SynPUF export is a **CDM 5.2-shaped** layout — it has no
  `visit_detail_id`, no `*_datetime`, and no `condition_status_*` columns — so
  its columns do **not** line up with the v5.3.1 DDL. Because `bq load` maps CSV
  columns **by position**, a v5.3 schema would drop every value into the wrong
  field (e.g. an ICD-9 source value landing in `visit_detail_id INT64`). Header
  mode reads each file's real column order and looks the type up by name, so the
  schema always matches the data on hand. Vocabulary tables are left on their
  DDL schemas (the Athena CSVs already match v5.3).

## Regenerating schemas

**`load` does this for you** (`CLINICAL_SKIP=1`, `REGEN_SCHEMAS=1` by default), so
the committed clinical schemas don't need to be kept in sync with the data — they
are rebuilt from the CSV headers on each load. To run it by hand:

Clinical tables — from the actual CSV headers (needs a header row, i.e. you'll
load with `CLINICAL_SKIP=1`):

```bash
python3 generate_schemas.py --from-csv ~/workspace/resources/cmsdesynpuf100k
```

Vocabulary tables (and a from-scratch v5.3.1 set) — from the DDL:

```bash
python3 generate_schemas.py
```

## After loading: diagnostics

`diagnostics.py` is a read-only survey of the workspace. It reports how to reach
BigQuery from here and what the data actually contains. Run it on the
Workbench after `./load_synpuf.sh load`:

```bash
cd staging
python3 diagnostics.py 2>&1 | tee diagnostics.txt
```

Options:

| Flag | Default | Purpose |
|------|---------|---------|
| `--project` | `$BQ_PROJECT`, then `$GOOGLE_CLOUD_PROJECT` | Project to query and bill |
| `--dataset` | `$BQ_DATASET`, then `synpuf_omop` | The loaded SynPUF dataset |
| `--test-write NAME` | off | Checks that we can create dataset `NAME` plus a table and a view in it. Deletes everything it created. |

It only needs `google-cloud-bigquery` and `pandas`. One failing section doesn't
stop the others. The last line reports the total bytes BigQuery processed.

What it checks, and why:

- **Environment and access paths.** Lists workbench environment variables,
  including whether the All of Us `WORKSPACE_CDR` convention exists here, the
  installed clients, `wb resource list`, and the visible datasets and their
  locations. It then tests the three common ways to get a DataFrame:
  `client.query().to_dataframe()`, the same call through the Storage API, and
  `pandas.read_gbq`, which is the call AoU's Dataset Builder generates.
- **A1C.** Builds the original A1C concept set in two forms: with its "excluded"
  concepts (the original's behavior) and without them. Shows which concepts
  actually occur in `measurement` and `procedure_occurrence`, and whether they
  carry values. The question is whether honoring the exclusions would leave the
  exercise with no A1C rows.
- **Pancreatic cancer.** Counts real rows under concept 42872399, and shows how
  ICD-9 157.x codes actually map.
- **Diabetes.** T2DM coverage, the most common concepts in the set (needed to
  rebuild the "complicated" and "uncomplicated" concept sets), and where ICD-9
  250.x codes map.
- **Death and survival.** Death-table size and duplicates, clinical activity
  recorded after death, overlap between decedents and T2DM, and T2DM follow-up
  quartiles (does five-year censoring ever apply?).
- **Location and IDs.** Whether locations are already populated, ID ranges, and
  whether the original `+16,000,000` offset on A1C `measurement_id` values would
  collide with existing IDs.

## Building the workshop dataset (once per workspace)

The original exercises ran on an OMOP-converted Synthea dataset. SynPUF's
deaths are unrelated to diagnoses and its lab rows have no values, so
`augment.py` adds the signal the exercises need:

- **Pancreatic cancer.** About 10% of decedents get a *Primary adenocarcinoma of
  pancreas* diagnosis, dated an exponential number of days (mean 300) before
  their death. A further ~10% of the pancreatic-cancer group is drawn from
  living people, with the diagnosis dated before their last recorded activity,
  so they come out censored. The death table is unchanged.
- **A1C values.** SynPUF already has A1C test rows (CPT 83036/83037) without
  values. Those rows are recoded to the standard concept their CPT code "Maps
  to" (the CPT code is kept as the source concept) and given a value. The value
  is drawn around 7.5 if the person had diabetes on or before the test date,
  and around 5.0 otherwise.
- **Hexagon locations.** SynPUF locations are counties with SSA state+county
  codes. `location` gains an `h3_id` column by translating SSA → county FIPS
  (NBER crosswalk) → the county's H3 resolution-3 hexagon. Locations that don't
  match get `NULL`: "unknown county" codes (`x999`), state code `54`, and
  Connecticut, whose old counties aren't in the hexagon crosswalk (it uses the
  2022 planning regions). The script prints match rates and the unmatched
  states.

The output is a separate dataset in which `condition_occurrence`,
`measurement` and `location` are real tables and every other table is a view onto
`synpuf_omop`. Notebooks query it like any OMOP dataset. Tables starting with
`_` record what was added and hold the crosswalks, for instructors.

```bash
# once: create the target dataset as a Workbench resource (UI or `wb resource create bq-dataset`),
# named synpuf_workshop, in the same location as synpuf_omop (us-central1)
cd staging
python3 augment.py              # idempotent and deterministic; re-running rebuilds the same data
python3 augment.py --print-sql  # see exactly what it runs
```

`notebooks/bq_env.py` works around a bug in the Workbench image: the image's `dsub`
pins `google-api-core` too low for the bundled BigQuery client, so the client
won't import. It reinstalls the image's intended `google-api-core` only when
the import fails. Both `augment.py` and the notebooks use it.

**Provisional:** the diabetes definition (`DIABETES_SEEDS` in `augment.py`) is
still pending the original "complicated" and "uncomplicated" concept sets. The
script warns if it finds far fewer diabetics than the ICD-9 250.x codes do. The
same definition appears in section 2.2 of `notebooks/survival_and_a1c.ipynb`;
keep the two in sync and re-run `augment.py` after any change.

What the diagnostics found, and why the augmentation looks the way it does, is
written up in [`docs/synpuf-data-findings.md`](../docs/synpuf-data-findings.md).
