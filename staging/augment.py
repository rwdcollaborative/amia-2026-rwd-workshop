#!/usr/bin/env python3
"""
augment.py -- build the workshop's OMOP dataset: SynPUF plus the synthetic
records the exercises rely on. Run ONCE per workspace (it is idempotent and
deterministic, so re-running rebuilds the identical dataset).

SynPUF is synthetic Medicare claims data. Its deaths are drawn independently of
diagnoses, and its lab rows carry no values, so the survival and A1C exercises
would show nothing on it as-is. This script adds just enough signal:

  1. Pancreatic cancer. A sample of decedents gets a "Primary adenocarcinoma of
     pancreas" diagnosis dated an exponentially distributed number of days
     (mean SURVIVAL_SCALE_DAYS) before their death. A smaller set of living
     people gets the same diagnosis dated before their last recorded activity,
     so some pancreatic-cancer patients are censored rather than all dying.
     The death table itself is not modified.
  2. A1C values. SynPUF already has A1C test rows (CPT 83036/83037) with no
     values. Those rows are recoded to the standard concept their CPT code
     "Maps to" in the vocabulary (the original code is kept as the source
     concept), and given a value: people with diabetes on or before the test
     date ~ 5.5 + sum of 4 U(0,1) (mean 7.5); everyone else
     ~ 3.5 + 0.75 * sum of 4 U(0,1) (mean 5.0).

  3. Hexagon locations. SynPUF locations are counties, coded with SSA
     state+county codes. location gains an h3_id column: SSA code -> county
     FIPS (NBER crosswalk) -> the H3 resolution-3 hexagon assigned to that
     county (staging/data/h3res3_countyfips_cw.csv, plus county_h3_supplement.csv
     for 2010-era counties that crosswalk lacks). Locations with no match
     (unknown-county codes, state code 54) get NULL.

Output dataset (default `synpuf_workshop`):
  - condition_occurrence, measurement,
    location                           : real tables (source rows + changes)
  - every other source table           : a view onto the source dataset
  - _pc_patients, _augment_manifest,
    _ssa_fips_crosswalk,
    _h3_county_crosswalk,
    _h3_county_supplement              : instructor-facing audit/lookup tables

Randomness comes from FARM_FINGERPRINT(seed:tag:id), so it is reproducible
and runs entirely inside BigQuery -- no data is pulled to the VM.

The target dataset must already exist (we can't create datasets through the
BigQuery API in the Workbench). Create it once as a Workbench resource, e.g.
in the UI: Resources -> new BigQuery dataset `synpuf_workshop`, location
us-central1 (same as synpuf_omop); or with `wb resource create bq-dataset`.

Usage:
  python3 augment.py                       # synpuf_omop -> synpuf_workshop
  python3 augment.py --target my_dataset
  python3 augment.py --print-sql           # show the SQL, run nothing
"""
import argparse
import csv
import json
import os
import sys

# bq_env.py lives with the notebooks, which use it too.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "notebooks"))
from bq_env import ensure_bigquery  # noqa: E402

bigquery = ensure_bigquery()

# ---------------------------------------------------------------------------
# Parameters (recorded in _augment_manifest)
# ---------------------------------------------------------------------------
SEED = "amia2026"

PC_CONCEPT_ID = 42872399          # Primary adenocarcinoma of pancreas
PC_DECEDENT_FRACTION = 0.10       # share of decedents given the diagnosis
PC_CENSORED_SHARE = 0.10          # share of PC patients drawn from living people
SURVIVAL_SCALE_DAYS = 300         # exponential mean of diagnosis -> death/last activity

# A1C rows as they exist in SynPUF (CPT4, non-standard in the current vocab).
A1C_SOURCE_CONCEPTS = [2212392, 2212393]   # CPT 83036, 83037

# The original exercise's A1C concept set: (concept_id, include_descendants, is_excluded).
# Recode targets must land inside its (exclusion-respecting) expansion.
A1C_CONCEPT_SET = [
    (4184637, True, False),
    (2212393, False, True),
    (724554, False, True),
    (3004410, True, False),
    (3003309, True, False),
    (3005673, True, False),
    (40765129, True, False),
    (40762352, True, False),
]

# Diabetes = union of the original "complicated" and "uncomplicated" concept
# sets, as seeds expanded with descendants. PROVISIONAL: seeds taken from an
# ATLAS search, pending the colleagues' actual concept-set definitions.
DIABETES_SEEDS = [
    442793,    # Complication due to diabetes mellitus
    4008576,   # Diabetes mellitus without complication
    # Added for SynPUF, which maps ICD-9 250.x to these broader concepts rather
    # than to anything under the two seeds above (they'd find ~65% of diabetics).
    201826,    # Type 2 diabetes mellitus
    201254,    # Type 1 diabetes mellitus
]

# Crosswalks for the hexagon step, in staging/data/.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SSA_FIPS_CSV = "ssa_fips_state_county2011.csv"   # NBER SSA <-> FIPS county crosswalk
H3_COUNTY_CSV = "h3res3_countyfips_cw.csv"       # county FIPS -> H3 res-3 hexagon
# The hexagon crosswalk uses current county FIPS; SynPUF is ~2010. This adds
# hexagons for counties that have changed since (CT's 2022 planning regions,
# renames/merges) or are missing from it (Honolulu). Used only where the main
# crosswalk has no row for a county.
H3_SUPPLEMENT_CSV = "county_h3_supplement.csv"

# Tables materialized here; everything else becomes a view.
MODIFIED = {"condition_occurrence", "measurement", "location"}

PRINT_ONLY = False
client = S = T = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def U(tag, id_expr):
    """SQL for a reproducible Uniform(0,1) draw keyed by (SEED, tag, id)."""
    fp = f"FARM_FINGERPRINT(CONCAT('{SEED}:{tag}:', CAST({id_expr} AS STRING)))"
    return f"((MOD(MOD({fp}, 1000000000) + 1000000000, 1000000000) + 0.5) / 1e9)"


def exp_days(tag, id_expr):
    """SQL for 1 + floor(Exponential(mean SURVIVAL_SCALE_DAYS)) days, keyed by id.
    The +1 keeps every interval positive (the analysis drops non-positive ones)."""
    return f"(1 + CAST(FLOOR(-LN(1 - {U(tag, id_expr)}) * {SURVIVAL_SCALE_DAYS}) AS INT64))"


def src(name):
    return f"`{S}.{name}`"


def tgt(name):
    return f"`{T}.{name}`"


def run(sql, label):
    print(f"\n-- {label}")
    if PRINT_ONLY:
        print(sql.strip() + ";")
        return []
    job = client.query(sql)
    rows = list(job.result())
    print(f"   ok ({(job.total_bytes_processed or 0) / 1e9:.2f} GB processed)")
    return rows


def columns(table):
    """[(name, type)] of a source table, in table order."""
    if PRINT_ONLY and client is None:
        raise SystemExit("--print-sql still needs BigQuery access to read table schemas.")
    return [(f.name, f.field_type) for f in client.get_table(f"{S}.{table}").schema]


BQ_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def select_list(cols, values):
    """SELECT list in the table's column order: given expressions, else typed NULLs."""
    unknown = set(values) - {c for c, _ in cols}
    assert not unknown, f"columns not in table: {unknown}"
    return ",\n  ".join(
        f"{values[c]} AS {c}" if c in values else f"CAST(NULL AS {BQ_TYPES.get(t, t)}) AS {c}"
        for c, t in cols)


def expand_sql(seeds_with_flags):
    """SQL for the exclusion-respecting expansion of a concept-set expression."""
    rows = ", ".join(f"STRUCT({c} AS concept_id, {str(d).upper()} AS incl_desc, {str(x).upper()} AS excluded)"
                     for c, d, x in seeds_with_flags)
    return f"""(
  WITH seeds AS (SELECT * FROM UNNEST([{rows}])),
  expanded AS (
    SELECT concept_id, excluded FROM seeds
    UNION ALL
    SELECT ca.descendant_concept_id, s.excluded
    FROM seeds s JOIN {src('concept_ancestor')} ca ON ca.ancestor_concept_id = s.concept_id
    WHERE s.incl_desc)
  SELECT DISTINCT concept_id FROM expanded WHERE NOT excluded
  EXCEPT DISTINCT
  SELECT concept_id FROM expanded WHERE excluded)"""


def first_diabetes_cte():
    seeds = ", ".join(map(str, DIABETES_SEEDS))
    return f"""first_dm AS (
  SELECT co.person_id, MIN(co.condition_start_date) AS first_dm_date
  FROM {src('condition_occurrence')} co
  JOIN {src('concept_ancestor')} ca ON ca.descendant_concept_id = co.condition_concept_id
  WHERE ca.ancestor_concept_id IN ({seeds})
  GROUP BY 1)"""


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
def check_datasets():
    s = client.get_dataset(S)
    try:
        t = client.get_dataset(T)
    except Exception:  # noqa: BLE001
        sys.exit(f"Target dataset {T} does not exist. Create it once as a Workbench resource\n"
                 f"(UI: Resources -> new BigQuery dataset, location {s.location}; or\n"
                 f"`wb resource create bq-dataset --help`), then re-run.")
    if s.location != t.location:
        sys.exit(f"{T} is in {t.location} but {S} is in {s.location}; views across datasets\n"
                 f"need the same location. Recreate {T} in {s.location}.")
    print(f"source {S} ({s.location}) -> target {T} ({t.location})")


def a1c_recode_map():
    """{source CPT concept: standard target} via 'Maps to', checked against the A1C set."""
    ids = ", ".join(map(str, A1C_SOURCE_CONCEPTS))
    rows = run(f"""
WITH a1c_set AS {expand_sql(A1C_CONCEPT_SET)}
SELECT cr.concept_id_1 AS source_id, cr.concept_id_2 AS target_id, c.concept_name,
       s.concept_id IS NOT NULL AS in_a1c_set
FROM {src('concept_relationship')} cr
JOIN {src('concept')} c ON c.concept_id = cr.concept_id_2
LEFT JOIN a1c_set s ON s.concept_id = cr.concept_id_2
WHERE cr.concept_id_1 IN ({ids}) AND cr.relationship_id = 'Maps to'
  AND cr.invalid_reason IS NULL""", "A1C: look up 'Maps to' targets")
    if PRINT_ONLY:
        return {cid: f"0 /* 'Maps to' target of {cid} */" for cid in A1C_SOURCE_CONCEPTS}
    mapping = {}
    for r in rows:
        print(f"   {r.source_id} -> {r.target_id} {r.concept_name!r} (in A1C set: {r.in_a1c_set})")
        if r.in_a1c_set:
            mapping.setdefault(r.source_id, r.target_id)
    # A code whose own target is outside the set (CPT 83037, the home-device test,
    # is standard and maps to itself -- a concept the original set excludes) is
    # recoded to the other A1C codes' in-set target, so every A1C row gets a value.
    targets = sorted(set(mapping.values()))
    if not targets:
        sys.exit("No A1C code maps into the A1C concept set; pick a target by hand.")
    for cid in sorted(set(A1C_SOURCE_CONCEPTS) - set(mapping)):
        mapping[cid] = targets[0]
        print(f"   {cid} has no in-set target; recoding it to {targets[0]} as well")
    return mapping


def build_pc_patients():
    sol = f"""
    SELECT person_id, MAX(dt) AS sol_date FROM (
      SELECT person_id, condition_start_date AS dt FROM {src('condition_occurrence')}
      UNION ALL SELECT person_id, measurement_date FROM {src('measurement')}
      UNION ALL SELECT person_id, procedure_date FROM {src('procedure_occurrence')})
    GROUP BY 1"""
    run(f"""
CREATE OR REPLACE TABLE {tgt('_pc_patients')} AS
WITH d AS (SELECT person_id, MIN(death_date) AS death_date FROM {src('death')} GROUP BY 1),
sol AS ({sol}),
decedents AS (
  SELECT person_id, 'decedent' AS kind, death_date AS end_date,
         DATE_SUB(death_date, INTERVAL {exp_days('pc_time', 'person_id')} DAY) AS dx_date
  FROM d WHERE {U('pc_pick', 'person_id')} < {PC_DECEDENT_FRACTION}),
living AS (
  SELECT sol.person_id, sol.sol_date
  FROM sol LEFT JOIN d USING (person_id) WHERE d.person_id IS NULL),
rate AS (  -- living sampling rate giving PC_CENSORED_SHARE of all PC patients
  SELECT (SELECT COUNT(*) FROM decedents) * {PC_CENSORED_SHARE} / (1 - {PC_CENSORED_SHARE})
         / (SELECT COUNT(*) FROM living) AS p),
censored AS (
  SELECT l.person_id, 'censored' AS kind, l.sol_date AS end_date,
         DATE_SUB(l.sol_date, INTERVAL {exp_days('pc_time', 'l.person_id')} DAY) AS dx_date
  FROM living l, rate WHERE {U('pc_pick_living', 'l.person_id')} < rate.p)
SELECT * FROM decedents UNION ALL SELECT * FROM censored""", "pancreatic cancer: pick patients")


def build_condition_occurrence():
    cols = columns("condition_occurrence")
    values = {
        "condition_occurrence_id":
            f"(SELECT MAX(condition_occurrence_id) FROM {src('condition_occurrence')})"
            " + ROW_NUMBER() OVER (ORDER BY pc.person_id)",
        "person_id": "pc.person_id",
        "condition_concept_id": str(PC_CONCEPT_ID),
        "condition_start_date": "pc.dx_date",
        "condition_start_datetime": "CAST(pc.dx_date AS DATETIME)",
        "condition_end_date": "pc.dx_date",
        "condition_end_datetime": "CAST(pc.dx_date AS DATETIME)",
    }
    run(f"""
CREATE OR REPLACE TABLE {tgt('condition_occurrence')} AS
SELECT * FROM {src('condition_occurrence')}
UNION ALL
SELECT
  {select_list(cols, values)}
FROM {tgt('_pc_patients')} pc""", "condition_occurrence: source rows + pancreatic-cancer diagnoses")


def build_measurement(recode):
    ids = ", ".join(map(str, A1C_SOURCE_CONCEPTS))
    is_a1c = f"m.measurement_concept_id IN ({ids})"
    target = "CASE m.measurement_concept_id " + " ".join(
        f"WHEN {s} THEN {t}" for s, t in recode.items()) + " END"
    u4 = " + ".join(U(f"a1c_{i}", "m.measurement_id") for i in range(1, 5))
    value = (f"ROUND(IF(dm.first_dm_date <= m.measurement_date, "
             f"{u4} + 5.5, ({u4}) * 3 / 4 + 3.5), 1)")
    run(f"""
CREATE OR REPLACE TABLE {tgt('measurement')} AS
WITH {first_diabetes_cte()}
SELECT m.* REPLACE (
  IF({is_a1c}, {target}, m.measurement_concept_id) AS measurement_concept_id,
  IF({is_a1c}, m.measurement_concept_id, m.measurement_source_concept_id) AS measurement_source_concept_id,
  IF({is_a1c}, {value}, m.value_as_number) AS value_as_number,
  IF({is_a1c}, '%', m.unit_source_value) AS unit_source_value)
FROM {src('measurement')} m
LEFT JOIN first_dm dm ON dm.person_id = m.person_id""", "measurement: recode A1C rows and fill values")


def drop_if_view(name):
    """An earlier build may have left a view where we now want a table."""
    if PRINT_ONLY:
        return
    try:
        existing = client.get_table(f"{T}.{name}")
    except Exception:  # noqa: BLE001 -- not found
        return
    if existing.table_type == "VIEW":
        client.delete_table(existing)


def load_csv(filename, table):
    """Load staging/data/<filename> into T.<table>, every column as STRING
    (keeps leading zeros in codes), replacing any previous copy."""
    path = os.path.join(DATA_DIR, filename)
    print(f"\n-- load {filename} -> {table}")
    if PRINT_ONLY:
        return
    with open(path, newline="") as f:
        header = next(csv.reader(f))
    config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV, skip_leading_rows=1,
        schema=[bigquery.SchemaField(c, "STRING") for c in header],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    with open(path, "rb") as f:
        job = client.load_table_from_file(f, f"{T}.{table}", job_config=config)
    job.result()
    print(f"   ok ({job.output_rows} rows)")


def build_location():
    load_csv(SSA_FIPS_CSV, "_ssa_fips_crosswalk")
    load_csv(H3_COUNTY_CSV, "_h3_county_crosswalk")
    load_csv(H3_SUPPLEMENT_CSV, "_h3_county_supplement")
    drop_if_view("location")
    run(f"""
CREATE OR REPLACE TABLE {tgt('location')} AS
WITH ssa AS (
  SELECT ssacounty, ANY_VALUE(fipscounty) AS fipscounty
  FROM {tgt('_ssa_fips_crosswalk')}
  WHERE ssacounty IS NOT NULL AND ssacounty != ''
  GROUP BY ssacounty),
main AS (
  SELECT county AS fipscounty, ANY_VALUE(h3_id) AS h3_id
  FROM {tgt('_h3_county_crosswalk')}
  WHERE county IS NOT NULL AND county != ''
  GROUP BY county),
h3 AS (
  SELECT * FROM main
  UNION ALL
  SELECT county, h3_id FROM {tgt('_h3_county_supplement')}
  WHERE county NOT IN (SELECT fipscounty FROM main))
SELECT l.*, h3.h3_id
FROM {src('location')} l
LEFT JOIN ssa ON ssa.ssacounty = LPAD(l.county, 5, '0')
LEFT JOIN h3 USING (fipscounty)""", "location: add h3_id (SSA county -> FIPS -> H3 res 3)")

    rows = run(f"""
SELECT COUNT(*) AS locations, COUNTIF(h3_id IS NOT NULL) AS locations_with_h3,
  (SELECT COUNT(*) FROM {src('person')}) AS persons,
  (SELECT COUNTIF(l.h3_id IS NOT NULL)
   FROM {src('person')} p JOIN {tgt('location')} l USING (location_id)) AS persons_with_h3,
  (SELECT COUNT(DISTINCT h3_id) FROM {tgt('location')}) AS distinct_hexes
FROM {tgt('location')}""", "location: match summary")
    if PRINT_ONLY:
        return {}
    stats = dict(rows[0])
    for k, v in stats.items():
        print(f"   {k:24s} {v}")
    print("\n   People without a hexagon, by SSA state code (top 10):")
    for r in run(f"""
SELECT l.state, COUNT(*) AS persons, COUNT(DISTINCT l.county) AS counties,
       ARRAY_AGG(DISTINCT l.county LIMIT 5) AS example_counties
FROM {src('person')} p JOIN {tgt('location')} l USING (location_id)
WHERE l.h3_id IS NULL
GROUP BY 1 ORDER BY persons DESC LIMIT 10""", "location: unmatched by state"):
        print(f"   state {r.state}: {r.persons:,} persons in {r.counties} counties, e.g. {list(r.example_counties)}")
    return {"location_" + k: v for k, v in stats.items()}


def build_views():
    names = sorted(t.table_id for t in client.list_tables(S)) if client else ["<every other table>"]
    for name in names:
        if name in MODIFIED:
            continue
        run(f"CREATE OR REPLACE VIEW {tgt(name)} AS SELECT * FROM {src(name)}", f"view {name}")


def report(recode, location_stats):
    rows = run(f"""
WITH {first_diabetes_cte()},
icd AS (SELECT COUNT(DISTINCT person_id) AS n FROM {src('condition_occurrence')}
        WHERE condition_source_value LIKE '250%'),
a1c AS (
  SELECT dm.first_dm_date <= m.measurement_date AS diabetic, m.value_as_number AS v
  FROM {tgt('measurement')} m LEFT JOIN first_dm dm USING (person_id)
  WHERE m.measurement_concept_id IN ({", ".join(str(t) for t in set(recode.values()))}))
SELECT
  (SELECT COUNTIF(kind = 'decedent') FROM {tgt('_pc_patients')}) AS pc_decedents,
  (SELECT COUNTIF(kind = 'censored') FROM {tgt('_pc_patients')}) AS pc_censored,
  (SELECT COUNT(*) FROM {tgt('_pc_patients')} pc JOIN first_dm USING (person_id)) AS pc_with_diabetes,
  (SELECT COUNT(*) FROM first_dm) AS persons_with_diabetes,
  (SELECT n FROM icd) AS persons_with_icd9_250,
  (SELECT COUNT(*) FROM a1c) AS a1c_rows,
  (SELECT ROUND(AVG(v), 2) FROM a1c WHERE diabetic) AS a1c_mean_diabetic,
  (SELECT ROUND(AVG(v), 2) FROM a1c WHERE diabetic IS NOT TRUE) AS a1c_mean_other""",
               "summary")
    if PRINT_ONLY:
        return
    r = dict(rows[0])
    for k, v in r.items():
        print(f"   {k:24s} {v}")
    cover = r["persons_with_diabetes"] / max(r["persons_with_icd9_250"], 1)
    if cover < 0.9:
        print(f"\n   WARNING: the diabetes concept set finds {cover:.0%} of the people with an\n"
              f"   ICD-9 250.x code. DIABETES_SEEDS is probably missing concepts (e.g. plain\n"
              f"   'Type 2 diabetes mellitus'); A1C values follow that definition.")

    manifest = {
        "seed": SEED, "source": S, "pc_concept_id": PC_CONCEPT_ID,
        "pc_decedent_fraction": PC_DECEDENT_FRACTION, "pc_censored_share": PC_CENSORED_SHARE,
        "survival_scale_days": SURVIVAL_SCALE_DAYS, "diabetes_seeds": DIABETES_SEEDS,
        "a1c_recode": {str(k): v for k, v in recode.items()}, **r, **location_stats,
    }
    rows_sql = ", ".join(
        f"STRUCT('{k}' AS key, '{v if isinstance(v, str) else json.dumps(v)}' AS value)"
        for k, v in manifest.items())
    run(f"CREATE OR REPLACE TABLE {tgt('_augment_manifest')} AS "
        f"SELECT CURRENT_TIMESTAMP() AS built_at, * FROM UNNEST([{rows_sql}])", "manifest")


# ---------------------------------------------------------------------------
def main():
    global client, S, T, PRINT_ONLY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default=os.environ.get("BQ_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--source", default="synpuf_omop", help="loaded SynPUF dataset (default: synpuf_omop)")
    ap.add_argument("--target", default="synpuf_workshop", help="dataset to build (default: synpuf_workshop)")
    ap.add_argument("--print-sql", action="store_true", help="print the SQL instead of running it")
    args = ap.parse_args()
    if not args.project:
        sys.exit("No project: set GOOGLE_CLOUD_PROJECT or pass --project.")
    S, T, PRINT_ONLY = f"{args.project}.{args.source}", f"{args.project}.{args.target}", args.print_sql
    client = bigquery.Client(project=args.project)

    if not PRINT_ONLY:
        check_datasets()
    recode = a1c_recode_map()
    build_pc_patients()
    build_condition_occurrence()
    build_measurement(recode)
    location_stats = build_location()
    build_views()
    report(recode, location_stats)
    print(f"\nDone. Notebooks should point at {T}.")


if __name__ == "__main__":
    main()
