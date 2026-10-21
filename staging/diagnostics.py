#!/usr/bin/env python3
"""
diagnostics.py -- read-only survey of this workspace's BigQuery setup and the
SynPUF OMOP data, to plan the workshop exercises (the once-per-workspace
augmentation step and the survival / A1C analyses built on it).

It answers two kinds of question:

  1. "How do we talk to BigQuery here?"  -- environment variables the workbench
     exports (incl. the AoU-style WORKSPACE_CDR), installed client packages,
     CLIs, visible datasets + their locations, and whether the common access
     paths (google-cloud-bigquery, pandas.read_gbq, BQ Storage API) work.
  2. "What's actually in the data?"      -- table shapes (SynPUF is CDM 5.2-ish),
     date coverage, and the specific facts the exercises depend on: A1C rows and
     their values, pancreatic-cancer and diabetes concepts, the death table,
     locations, and primary-key ranges.

Everything is read-only unless you pass --test-write.

Usage:
  python3 diagnostics.py                              # $GOOGLE_CLOUD_PROJECT, dataset synpuf_omop
  python3 diagnostics.py --dataset synpuf --project my-proj
  python3 diagnostics.py --test-write synpuf_workshop # also check we can create a dataset/table
  python3 diagnostics.py 2>&1 | tee diagnostics.txt   # keep a copy to share
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import warnings
from importlib import metadata

# google-api-core warns (once per package) that grpcio < 1.83 lacks post-quantum
# crypto support. Harmless here, and it buries the report.
warnings.filterwarnings("ignore", message=".*Post-Quantum Cryptography.*", category=FutureWarning)

import pandas as pd

# Imported defensively: a pip install that upgrades google-cloud-bigquery without
# its google-api-core dependency breaks this import, and we still want the
# environment report (section 1) to show which versions are out of step.
try:
    from google.cloud import bigquery
    BQ_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001
    bigquery = None
    BQ_IMPORT_ERROR = f"{type(e).__name__}: {e}"

# ---------------------------------------------------------------------------
# Concepts the original (Foundry) exercises used
# ---------------------------------------------------------------------------
T2DM_ID = 201826       # Type 2 diabetes mellitus (SNOMED)
PC_ID = 42872399       # Primary adenocarcinoma of pancreas
MEASUREMENT_ID_OFFSET = 16_000_000

# A1C concept-set seeds as recorded in the original workbook:
# (concept_id, include_descendants, is_excluded). The original ignored
# is_excluded, so the "original" set includes the excluded rows too.
A1C_SEEDS = [
    (4184637, True, False),
    (2212393, False, True),
    (724554, False, True),
    (3004410, True, False),
    (3003309, True, False),
    (3005673, True, False),
    (40765129, True, False),
    (40762352, True, False),
]

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------
P = D = None            # project, dataset (set in main)
client = None
BYTES = 0               # total bytes processed, for a cost sanity check
SECRET_PAT = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY$|APIKEY", re.I)


def t(name):
    """Backtick-quoted fully-qualified table ref."""
    return f"`{P}.{D}.{name}`"


def q(sql):
    """Run SQL, return a DataFrame. Builds the frame from plain rows so it
    doesn't depend on db-dtypes being installed."""
    global BYTES
    job = client.query(sql)
    rows = list(job.result())
    BYTES += job.total_bytes_processed or 0
    return pd.DataFrame([dict(r) for r in rows])


def show(df, max_rows=40):
    if df is None or len(df) == 0:
        print("  (no rows)")
        return
    with pd.option_context("display.width", 200, "display.max_columns", 50,
                           "display.max_colwidth", 60):
        text = df.head(max_rows).to_string(index=False)
    print("\n".join("  " + line for line in text.splitlines()))
    if len(df) > max_rows:
        print(f"  ... ({len(df) - max_rows} more rows)")


def note(msg):
    print(f"  -> {msg}")


def seeds_sql(seeds):
    structs = ",\n    ".join(
        f"STRUCT({c} AS concept_id, {str(d).upper()} AS incl_desc, {str(x).upper()} AS excluded)"
        for c, d, x in seeds)
    return f"SELECT * FROM UNNEST([\n    {structs}\n  ])"


def a1c_set_cte():
    """CTEs: a1c_orig (all listed + descendants, exclusions ignored -- the
    original behaviour) and a1c_fixed (excluded concepts + descendants removed)."""
    ca = t("concept_ancestor")
    return f"""
seeds AS ({seeds_sql(A1C_SEEDS)}),
expanded AS (
  SELECT concept_id, excluded FROM seeds
  UNION ALL
  SELECT ca.descendant_concept_id, s.excluded
  FROM seeds s JOIN {ca} ca ON ca.ancestor_concept_id = s.concept_id
  WHERE s.incl_desc
),
a1c_orig AS (SELECT DISTINCT concept_id FROM expanded),
a1c_fixed AS (
  SELECT DISTINCT concept_id FROM expanded WHERE NOT excluded
  EXCEPT DISTINCT
  SELECT concept_id FROM expanded WHERE excluded
)"""


# ---------------------------------------------------------------------------
# Section 1: environment
# ---------------------------------------------------------------------------
def env_vars():
    pat = re.compile(r"GOOGLE|WORKSPACE|CDR|BIGQUERY|WORKBENCH|^WB_|GCS|CLOUDSDK|TERRA|OWNER|BUCKET", re.I)
    found = {k: v for k, v in os.environ.items() if pat.search(k)}
    if not found:
        note("no GOOGLE_/WORKSPACE_/WORKBENCH_/CDR-style variables set")
    for k in sorted(found):
        v = "<redacted>" if SECRET_PAT.search(k) else found[k]
        print(f"  {k} = {v}")
    if "WORKSPACE_CDR" in os.environ:
        note("WORKSPACE_CDR is set: this is the All of Us convention for 'the CDR dataset'.")
    else:
        note("WORKSPACE_CDR not set (AoU Researcher Workbench sets it; we could mirror it).")


def packages():
    dists = ["google-cloud-bigquery", "google-api-core", "google-cloud-core", "google-auth",
             "google-cloud-bigquery-storage", "pandas",
             "pandas-gbq", "db-dtypes", "bigquery-magics", "pyarrow", "numpy",
             "scipy", "matplotlib", "seaborn", "lifelines", "google-cloud-storage"]
    print(f"  python {sys.version.split()[0]} ({sys.executable})")
    for d in dists:
        try:
            v = metadata.version(d)
        except metadata.PackageNotFoundError:
            v = "NOT INSTALLED"
        print(f"  {d:32s} {v}")


def clis():
    for c in ["bq", "gsutil", "gcloud", "wb", "jupyter"]:
        print(f"  {c:8s} {shutil.which(c) or 'not found'}")
    if shutil.which("wb"):
        for args in (["wb", "status"], ["wb", "resource", "list"]):
            print(f"\n  $ {' '.join(args)}")
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=90)
                text = (out.stdout + out.stderr).strip() or "(no output)"
                print("\n".join("    " + line for line in text.splitlines()[:60]))
            except Exception as e:  # noqa: BLE001
                print(f"    failed: {e}")


# ---------------------------------------------------------------------------
# Section 2: BigQuery access paths
# ---------------------------------------------------------------------------
def datasets():
    print(f"  client project: {client.project}")
    rows = []
    for item in client.list_datasets():
        ds = client.get_dataset(item.reference)
        n = sum(1 for _ in client.list_tables(ds.reference))
        rows.append({"dataset": ds.dataset_id, "location": ds.location, "tables": n})
    show(pd.DataFrame(rows))
    cdr = os.environ.get("WORKSPACE_CDR")
    if cdr:
        try:
            ds = client.get_dataset(cdr)
            note(f"WORKSPACE_CDR {cdr} readable, location {ds.location}")
        except Exception as e:  # noqa: BLE001
            note(f"WORKSPACE_CDR {cdr} NOT readable: {e}")


def access_paths():
    sql = f"SELECT person_id, year_of_birth FROM {t('person')} LIMIT 1000"

    t0 = time.time()
    df = client.query(sql).to_dataframe(create_bqstorage_client=False)
    print(f"  client.query().to_dataframe() [REST]      ok, {len(df)} rows, {time.time() - t0:.1f}s")

    try:
        t0 = time.time()
        df = client.query(sql).to_dataframe(create_bqstorage_client=True)
        print(f"  client.query().to_dataframe() [Storage]   ok, {len(df)} rows, {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        print(f"  client.query().to_dataframe() [Storage]   FAILED: {type(e).__name__}: {e}")

    # The AoU Dataset Builder emits pandas.read_gbq(...) with these exact args.
    try:
        import warnings
        t0 = time.time()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            df = pd.read_gbq(sql, dialect="standard", project_id=P,
                             use_bqstorage_api=("BIGQUERY_STORAGE_API_ENABLED" in os.environ),
                             progress_bar_type=None)
        msg = f"ok, {len(df)} rows, {time.time() - t0:.1f}s"
        if any("deprecated" in str(x.message).lower() for x in w):
            msg += "  (DeprecationWarning: pandas.read_gbq -> pandas_gbq.read_gbq)"
        print(f"  pandas.read_gbq (AoU style)               {msg}")
    except Exception as e:  # noqa: BLE001
        print(f"  pandas.read_gbq (AoU style)               FAILED: {type(e).__name__}: {e}")

    # DATE columns: does to_dataframe work without db-dtypes?
    try:
        df = client.query(f"SELECT death_date FROM {t('death')} LIMIT 5").to_dataframe(
            create_bqstorage_client=False)
        print(f"  DATE column -> pandas                     ok, dtype {df['death_date'].dtype}")
    except Exception as e:  # noqa: BLE001
        print(f"  DATE column -> pandas                     FAILED: {type(e).__name__}: {e}")


def test_write(name):
    ref = f"{P}.{name}"
    created = False
    try:
        client.get_dataset(ref)
        note(f"dataset {ref} already exists")
    except Exception:  # noqa: BLE001
        try:
            src = client.get_dataset(f"{P}.{D}")
            ds = bigquery.Dataset(ref)
            ds.location = src.location
            client.create_dataset(ds)
            created = True
            note(f"created dataset {ref} in {src.location} -> datasets.create OK")
        except Exception as e:  # noqa: BLE001
            note(f"cannot create dataset {ref}: {e}")
            return
    tbl = f"`{ref}._diag_write_test`"
    try:
        q(f"CREATE OR REPLACE TABLE {tbl} AS SELECT * FROM {t('person')} LIMIT 10")
        q(f"CREATE OR REPLACE VIEW `{ref}._diag_view_test` AS SELECT * FROM {t('person')}")
        note("CREATE TABLE AS SELECT and CREATE VIEW across datasets: OK")
    except Exception as e:  # noqa: BLE001
        note(f"write test failed: {e}")
    finally:
        client.delete_table(f"{ref}._diag_write_test", not_found_ok=True)
        client.delete_table(f"{ref}._diag_view_test", not_found_ok=True)
        if created:
            client.delete_dataset(ref, not_found_ok=True)
            note(f"cleaned up: deleted {ref}")


# ---------------------------------------------------------------------------
# Section 3: data shape
# ---------------------------------------------------------------------------
KEY_TABLES = ["person", "death", "condition_occurrence", "measurement",
              "procedure_occurrence", "observation", "location", "observation_period",
              "visit_occurrence", "drug_exposure", "device_exposure",
              "concept", "concept_ancestor"]


def tables():
    show(q(f"""
SELECT table_id, row_count, ROUND(size_bytes / 1e9, 2) AS size_gb
FROM {t('__TABLES__')} ORDER BY row_count DESC"""), max_rows=60)


def columns():
    names = ", ".join(f"'{n}'" for n in KEY_TABLES)
    df = q(f"""
SELECT table_name, column_name, data_type, ordinal_position
FROM `{P}.{D}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name IN ({names})
ORDER BY table_name, ordinal_position""")
    for name, g in df.groupby("table_name", sort=False):
        cols = ", ".join(f"{c}:{dt}" for c, dt in zip(g.column_name, g.data_type))
        print(f"  {name}: {cols}\n")
    missing = sorted(set(KEY_TABLES) - set(df.table_name))
    if missing:
        note(f"MISSING tables: {missing}")


def date_coverage():
    specs = [("condition_occurrence", "condition_start_date"),
             ("measurement", "measurement_date"),
             ("procedure_occurrence", "procedure_date"),
             ("observation", "observation_date"),
             ("visit_occurrence", "visit_start_date"),
             ("drug_exposure", "drug_exposure_start_date"),
             ("death", "death_date"),
             ("observation_period", "observation_period_start_date"),
             ("observation_period", "observation_period_end_date")]
    frames = []
    for tb, col in specs:  # one query each, so a missing table/column costs one row, not the section
        try:
            frames.append(q(f"""SELECT '{tb}' AS tbl, '{col}' AS col, CAST(MIN({col}) AS STRING) AS min_date,
       CAST(MAX({col}) AS STRING) AS max_date, COUNTIF({col} IS NULL) AS n_null, COUNT(*) AS n
FROM {t(tb)}"""))
        except Exception as e:  # noqa: BLE001
            frames.append(pd.DataFrame([{"tbl": tb, "col": col, "min_date": f"ERROR: {type(e).__name__}"}]))
    show(pd.concat(frames, ignore_index=True))


# ---------------------------------------------------------------------------
# Section 4: facts the exercises depend on
# ---------------------------------------------------------------------------
def concept_info(ids):
    return q(f"""
SELECT concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
       standard_concept, concept_code, invalid_reason
FROM {t('concept')} WHERE concept_id IN ({', '.join(map(str, ids))})""")


def a1c():
    print("  Seed concepts (as listed in the original workbook):")
    seeds = pd.DataFrame(A1C_SEEDS, columns=["concept_id", "incl_desc", "excluded"])
    show(seeds.merge(concept_info(seeds.concept_id.tolist()), how="left", on="concept_id"))

    print("\n  Concept-set sizes:")
    show(q(f"""WITH {a1c_set_cte()}
SELECT (SELECT COUNT(*) FROM a1c_orig) AS n_original_mode,
       (SELECT COUNT(*) FROM a1c_fixed) AS n_corrected_mode"""))

    print("\n  measurement rows matching the A1C set, by concept:")
    show(q(f"""WITH {a1c_set_cte()}
SELECT m.measurement_concept_id, c.concept_name, c.vocabulary_id,
       f.concept_id IS NOT NULL AS in_corrected,
       COUNT(*) AS n_rows, COUNT(DISTINCT m.person_id) AS n_persons,
       COUNTIF(m.value_as_number IS NOT NULL) AS n_with_value,
       ROUND(AVG(m.value_as_number), 2) AS mean_value,
       ROUND(MIN(m.value_as_number), 2) AS min_value,
       ROUND(MAX(m.value_as_number), 2) AS max_value
FROM {t('measurement')} m
JOIN a1c_orig s ON s.concept_id = m.measurement_concept_id
LEFT JOIN a1c_fixed f ON f.concept_id = m.measurement_concept_id
LEFT JOIN {t('concept')} c ON c.concept_id = m.measurement_concept_id
GROUP BY 1, 2, 3, 4 ORDER BY n_rows DESC"""))

    print("\n  procedure_occurrence rows matching the A1C set (claims labs can land here):")
    show(q(f"""WITH {a1c_set_cte()}
SELECT p.procedure_concept_id, c.concept_name, COUNT(*) AS n_rows,
       COUNT(DISTINCT p.person_id) AS n_persons
FROM {t('procedure_occurrence')} p
JOIN a1c_orig s ON s.concept_id = p.procedure_concept_id
LEFT JOIN {t('concept')} c ON c.concept_id = p.procedure_concept_id
GROUP BY 1, 2 ORDER BY n_rows DESC"""))

    print("\n  Rows by A1C *source* code (CPT 83036/83037, LOINC 4548-4/17856-6), any concept:")
    show(q(f"""
SELECT 'measurement' AS tbl, measurement_source_value AS source_value,
       measurement_concept_id AS concept_id, COUNT(*) AS n_rows,
       COUNTIF(value_as_number IS NOT NULL) AS n_with_value
FROM {t('measurement')}
WHERE measurement_source_value IN ('83036', '83037', '4548-4', '17856-6')
GROUP BY 1, 2, 3
UNION ALL
SELECT 'procedure_occurrence', procedure_source_value, procedure_concept_id, COUNT(*), NULL
FROM {t('procedure_occurrence')}
WHERE procedure_source_value IN ('83036', '83037', '4548-4', '17856-6')
GROUP BY 1, 2, 3
ORDER BY n_rows DESC"""))

    print("\n  How often does measurement carry a numeric value at all?")
    show(q(f"""
SELECT COUNT(*) AS n_rows, COUNTIF(value_as_number IS NOT NULL) AS n_with_value,
       COUNT(DISTINCT measurement_concept_id) AS n_concepts
FROM {t('measurement')}"""))


def diabetes():
    print(f"  Descendants of {T2DM_ID} (Type 2 DM) in condition_occurrence:")
    show(q(f"""
WITH s AS (SELECT descendant_concept_id AS cid FROM {t('concept_ancestor')}
           WHERE ancestor_concept_id = {T2DM_ID})
SELECT (SELECT COUNT(*) FROM s) AS n_concepts_in_set,
       COUNT(*) AS n_rows, COUNT(DISTINCT co.person_id) AS n_persons
FROM {t('condition_occurrence')} co JOIN s ON s.cid = co.condition_concept_id"""))

    print("\n  Top T2DM-set concepts by persons (candidates for 'complicated' vs 'uncomplicated'):")
    show(q(f"""
WITH s AS (SELECT descendant_concept_id AS cid FROM {t('concept_ancestor')}
           WHERE ancestor_concept_id = {T2DM_ID})
SELECT co.condition_concept_id, c.concept_name,
       COUNT(DISTINCT co.person_id) AS n_persons, COUNT(*) AS n_rows
FROM {t('condition_occurrence')} co JOIN s ON s.cid = co.condition_concept_id
JOIN {t('concept')} c ON c.concept_id = co.condition_concept_id
GROUP BY 1, 2 ORDER BY n_persons DESC LIMIT 25"""))

    print("\n  ICD-9 250.x source codes and where they map (anything outside the T2DM set?):")
    show(q(f"""
WITH s AS (SELECT descendant_concept_id AS cid FROM {t('concept_ancestor')}
           WHERE ancestor_concept_id = {T2DM_ID})
SELECT co.condition_concept_id, c.concept_name,
       s.cid IS NOT NULL AS in_t2dm_set,
       COUNT(DISTINCT co.person_id) AS n_persons
FROM {t('condition_occurrence')} co
LEFT JOIN s ON s.cid = co.condition_concept_id
LEFT JOIN {t('concept')} c ON c.concept_id = co.condition_concept_id
WHERE co.condition_source_value LIKE '250%'
GROUP BY 1, 2, 3 ORDER BY n_persons DESC LIMIT 25"""))


def pancreatic_cancer():
    print(f"  Seed concept {PC_ID}:")
    show(concept_info([PC_ID]))

    print("\n  Its ancestors (to see what broader concepts real data might use):")
    show(q(f"""
SELECT ca.ancestor_concept_id, c.concept_name, c.standard_concept,
       ca.min_levels_of_separation AS levels_up
FROM {t('concept_ancestor')} ca JOIN {t('concept')} c ON c.concept_id = ca.ancestor_concept_id
WHERE ca.descendant_concept_id = {PC_ID} AND ca.min_levels_of_separation BETWEEN 1 AND 3
ORDER BY levels_up, concept_name"""))

    print(f"\n  condition_occurrence rows in the descendant set of {PC_ID}:")
    show(q(f"""
WITH s AS (SELECT descendant_concept_id AS cid FROM {t('concept_ancestor')}
           WHERE ancestor_concept_id = {PC_ID})
SELECT (SELECT COUNT(*) FROM s) AS n_concepts_in_set,
       COUNT(*) AS n_rows, COUNT(DISTINCT co.person_id) AS n_persons
FROM {t('condition_occurrence')} co JOIN s ON s.cid = co.condition_concept_id"""))

    print("\n  ICD-9 157.x (malignant neoplasm of pancreas) source codes and where they map:")
    show(q(f"""
SELECT co.condition_source_value, co.condition_concept_id, c.concept_name,
       COUNT(DISTINCT co.person_id) AS n_persons, COUNT(*) AS n_rows
FROM {t('condition_occurrence')} co
LEFT JOIN {t('concept')} c ON c.concept_id = co.condition_concept_id
WHERE co.condition_source_value LIKE '157%'
GROUP BY 1, 2, 3 ORDER BY n_persons DESC LIMIT 25"""))


def death():
    show(q(f"""
SELECT COUNT(*) AS n_rows, COUNT(DISTINCT person_id) AS n_persons,
       COUNT(*) - COUNT(DISTINCT CONCAT(CAST(person_id AS STRING), '|', CAST(death_date AS STRING)))
         AS n_dup_person_date,
       COUNTIF(death_date IS NULL) AS n_null_date,
       COUNTIF(cause_concept_id IS NOT NULL AND cause_concept_id != 0) AS n_with_cause,
       ROUND(100 * COUNT(DISTINCT person_id) / (SELECT COUNT(*) FROM {t('person')}), 2) AS pct_of_persons
FROM {t('death')}"""))

    print("\n  death_type_concept_id distribution:")
    show(q(f"""
SELECT d.death_type_concept_id, c.concept_name, COUNT(*) AS n
FROM {t('death')} d LEFT JOIN {t('concept')} c ON c.concept_id = d.death_type_concept_id
GROUP BY 1, 2 ORDER BY n DESC"""))

    print("\n  Deaths per year:")
    show(q(f"""
SELECT EXTRACT(YEAR FROM death_date) AS year, COUNT(*) AS n
FROM {t('death')} GROUP BY 1 ORDER BY 1"""))

    print("\n  Decedents: clinical activity after death, and overlap with T2DM:")
    show(q(f"""
WITH d AS (SELECT person_id, MIN(death_date) AS death_date FROM {t('death')} GROUP BY 1),
sol AS (
  SELECT person_id, MAX(dt) AS sol_date FROM (
    SELECT person_id, condition_start_date AS dt FROM {t('condition_occurrence')}
    UNION ALL SELECT person_id, measurement_date FROM {t('measurement')}
    UNION ALL SELECT person_id, procedure_date FROM {t('procedure_occurrence')})
  GROUP BY 1),
dm AS (SELECT DISTINCT co.person_id FROM {t('condition_occurrence')} co
       JOIN {t('concept_ancestor')} ca ON ca.descendant_concept_id = co.condition_concept_id
       WHERE ca.ancestor_concept_id = {T2DM_ID})
SELECT COUNT(*) AS n_decedents,
       COUNTIF(sol.sol_date IS NULL) AS n_no_activity,
       COUNTIF(sol.sol_date > d.death_date) AS n_activity_after_death,
       COUNTIF(dm.person_id IS NOT NULL) AS n_with_t2dm,
       ROUND(100 * COUNTIF(dm.person_id IS NOT NULL) / COUNT(*), 1) AS pct_with_t2dm
FROM d LEFT JOIN sol USING (person_id) LEFT JOIN dm USING (person_id)"""))


def survival_preview():
    print("  T2DM persons: follow-up from first T2DM date to sign-of-life (cond/meas/proc) or death:")
    show(q(f"""
WITH dm AS (
  SELECT co.person_id, MIN(co.condition_start_date) AS dm_start
  FROM {t('condition_occurrence')} co
  JOIN {t('concept_ancestor')} ca ON ca.descendant_concept_id = co.condition_concept_id
  WHERE ca.ancestor_concept_id = {T2DM_ID} GROUP BY 1),
sol AS (
  SELECT person_id, MAX(dt) AS sol_date FROM (
    SELECT person_id, condition_start_date AS dt FROM {t('condition_occurrence')}
    UNION ALL SELECT person_id, measurement_date FROM {t('measurement')}
    UNION ALL SELECT person_id, procedure_date FROM {t('procedure_occurrence')})
  GROUP BY 1),
d AS (SELECT person_id, MIN(death_date) AS death_date FROM {t('death')} GROUP BY 1),
f AS (
  SELECT dm.person_id, d.death_date IS NOT NULL AS died,
         DATE_DIFF(COALESCE(d.death_date, sol.sol_date), dm.dm_start, DAY) AS days
  FROM dm LEFT JOIN sol USING (person_id) LEFT JOIN d USING (person_id))
SELECT COUNT(*) AS n, COUNTIF(died) AS n_died,
       COUNTIF(days <= 0) AS n_nonpositive, COUNTIF(days > 1825) AS n_over_5y,
       APPROX_QUANTILES(days, 4) AS days_quartiles
FROM f"""))


def location():
    show(q(f"""
SELECT COUNT(*) AS n_rows, COUNT(DISTINCT location_id) AS n_ids,
       COUNT(DISTINCT state) AS n_states, COUNTIF(county IS NOT NULL) AS n_with_county,
       COUNTIF(zip IS NOT NULL) AS n_with_zip, COUNTIF(city IS NOT NULL) AS n_with_city
FROM {t('location')}"""))
    print("\n  Sample rows:")
    show(q(f"SELECT * FROM {t('location')} LIMIT 5"))
    print("\n  person.location_id coverage:")
    show(q(f"""
SELECT COUNT(*) AS n_persons, COUNTIF(p.location_id IS NULL) AS n_null_location,
       COUNT(DISTINCT p.location_id) AS n_distinct_locations,
       COUNTIF(p.location_id IS NOT NULL AND l.location_id IS NULL) AS n_dangling_fk
FROM {t('person')} p
LEFT JOIN (SELECT DISTINCT location_id FROM {t('location')}) l ON l.location_id = p.location_id"""))


def ids():
    show(q(f"""
SELECT 'measurement_id' AS id, MIN(measurement_id) AS min_id, MAX(measurement_id) AS max_id,
       COUNT(*) AS n, COUNT(DISTINCT measurement_id) AS n_distinct FROM {t('measurement')}
UNION ALL
SELECT 'condition_occurrence_id', MIN(condition_occurrence_id), MAX(condition_occurrence_id),
       COUNT(*), COUNT(DISTINCT condition_occurrence_id) FROM {t('condition_occurrence')}
UNION ALL
SELECT 'procedure_occurrence_id', MIN(procedure_occurrence_id), MAX(procedure_occurrence_id),
       COUNT(*), COUNT(DISTINCT procedure_occurrence_id) FROM {t('procedure_occurrence')}
UNION ALL
SELECT 'person_id', MIN(person_id), MAX(person_id), COUNT(*), COUNT(DISTINCT person_id)
FROM {t('person')}"""))
    print(f"\n  Would the original +{MEASUREMENT_ID_OFFSET:,} offset on A1C rows collide?")
    show(q(f"""WITH {a1c_set_cte()},
shifted AS (SELECT measurement_id + {MEASUREMENT_ID_OFFSET} AS nid
            FROM {t('measurement')} m JOIN a1c_orig s ON s.concept_id = m.measurement_concept_id)
SELECT COUNT(*) AS n_shifted, COUNTIF(m.measurement_id IS NOT NULL) AS n_collisions
FROM shifted LEFT JOIN {t('measurement')} m ON m.measurement_id = shifted.nid"""))


# ---------------------------------------------------------------------------
def main():
    global P, D, client
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
                    or os.environ.get("GOOGLE_PROJECT"))
    ap.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "synpuf_omop"))
    ap.add_argument("--test-write", metavar="DATASET",
                    help="check we can create DATASET (deleted again if we created it) and write tables/views")
    args = ap.parse_args()
    if not args.project:
        sys.exit("No project: set GOOGLE_CLOUD_PROJECT or pass --project.")
    P, D = args.project, args.dataset

    sections = [
        ("1a. Environment variables", env_vars),
        ("1b. Python packages", packages),
        ("1c. Command-line tools (+ wb resources)", clis),
    ]
    if BQ_IMPORT_ERROR:
        run_sections(sections)
        print(f"\n{'=' * 78}")
        print(f"STOPPED: `from google.cloud import bigquery` failed:\n  {BQ_IMPORT_ERROR}")
        print("Usually google-cloud-bigquery is newer than google-api-core (compare the versions\n"
              "in 1b). Check where they're installed with:\n"
              "  python3 -m pip show google-cloud-bigquery google-api-core | grep -E 'Name|Version|Location'")
        sys.exit(1)
    client = bigquery.Client(project=P)

    sections += [
        ("2a. Datasets visible in the project", datasets),
        ("2b. Query -> DataFrame access paths", access_paths),
    ]
    if args.test_write:
        sections.append((f"2c. Write test ({args.test_write})", lambda: test_write(args.test_write)))
    sections += [
        ("3a. Tables and row counts", tables),
        ("3b. Columns of key tables (SynPUF is CDM 5.2-shaped)", columns),
        ("3c. Date coverage", date_coverage),
        ("4a. A1C concepts and measurements", a1c),
        ("4b. Diabetes", diabetes),
        ("4c. Pancreatic cancer", pancreatic_cancer),
        ("4d. Death table", death),
        ("4e. Survival preview (T2DM follow-up)", survival_preview),
        ("4f. Location", location),
        ("4g. Primary-key ranges", ids),
    ]

    failed = run_sections(sections)
    print(f"\n{'=' * 78}")
    print(f"Done. BigQuery bytes processed: {BYTES / 1e9:.2f} GB")
    if failed:
        print(f"Sections that failed: {failed}")


def run_sections(sections):
    print(f"diagnostics.py  project={P}  dataset={D}  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    failed = []
    for title, fn in sections:
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- keep going; one bad section shouldn't hide the rest
            failed.append(title)
            print(f"  SECTION FAILED: {type(e).__name__}: {e}")
            traceback.print_exc(limit=2, file=sys.stdout)
    return failed


if __name__ == "__main__":
    main()
