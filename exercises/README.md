# Workshop exercises

Exercises ported from an earlier (Palantir Foundry) run of this workshop:
a five-year survival comparison of pancreatic-cancer-only vs. diabetes-only
patients, and a comparison of A1C values by diabetes status. Both run on the
SynPUF OMOP data that `staging/` loads into BigQuery, plus a small amount of
synthetic data added once per workspace (synthetic pancreatic-cancer diagnoses
and A1C values, which SynPUF claims data doesn't have).

> Status: **planning**. Only the diagnostics script exists so far.

## 1. Diagnostics (run this first)

`diagnostics.py` is a read-only survey of the workspace. It reports how to reach
BigQuery from here and what the data actually contains. Run it on the
Workbench after `staging/load_synpuf.sh load`:

```bash
cd exercises
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

## 2. Build the workshop dataset (once per workspace)

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

The output is a separate dataset in which `condition_occurrence` and
`measurement` are real tables and every other table is a view onto
`synpuf_omop`. Notebooks query it like any OMOP dataset. Two tables starting
with `_` record what was added, for instructors.

```bash
# once: create the target dataset as a Workbench resource (UI or `wb resource create bq-dataset`),
# named synpuf_workshop, in the same location as synpuf_omop (us-central1)
cd exercises
python3 augment.py              # idempotent and deterministic; re-running rebuilds the same data
python3 augment.py --print-sql  # see exactly what it runs
```

`bq_env.py` works around a bug in the Workbench image: the image's `dsub`
pins `google-api-core` too low for the bundled BigQuery client, so the client
won't import. It reinstalls the image's intended `google-api-core` only when
the import fails. Both `augment.py` and the notebooks use it.

**Provisional:** the diabetes definition (`DIABETES_SEEDS` in `augment.py`) is
still pending the original "complicated" and "uncomplicated" concept sets. The
script warns if it finds far fewer diabetics than the ICD-9 250.x codes do.

## 3. The exercise notebook

`survival_and_a1c.ipynb` runs both analyses against `synpuf_workshop`:

1. **Survival.** Pancreatic-cancer-only vs. type-2-diabetes-only cohorts built
   from concept sets, sign-of-life dates, censoring, Kaplan–Meier curves, and a
   log-rank test.
2. **A1C.** Concept sets with descendants and exclusions, per-person A1C
   summaries, and a box plot by diabetes status.

Every query goes through a single `query(sql)` helper (`pandas_gbq.read_gbq`)
and a `CDR` dataset variable, mirroring the All of Us convention
(`os.environ["WORKSPACE_CDR"]`). The first cell applies the `bq_env.py` fix and
installs `lifelines` if it's missing.

The diabetes concept sets in section 2.2 must match `DIABETES_SEEDS` in
`augment.py`, because the A1C values were generated from that definition.
