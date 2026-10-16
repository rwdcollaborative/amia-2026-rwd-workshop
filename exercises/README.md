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

## 2. Planned shape (to be confirmed by the diagnostics)

- **Augmentation runs once per workspace and stays out of sight.** A staging
  step builds a separate dataset (e.g. `synpuf_workshop`). The modified tables
  (`person`, `condition_occurrence`, `measurement`, `death`) are materialized
  there. Every other table, vocabulary included, is a view onto `synpuf_omop`.
  Random draws are seeded, so re-running the step reproduces the same data.
- **Analysis notebooks see one ordinary OMOP dataset.** They name it the way an
  All of Us notebook does, through a single `CDR` variable resolved from
  `WORKSPACE_CDR` when it is set. That way the code reads like AoU code, and
  participants never see that some of the rows are synthetic.
