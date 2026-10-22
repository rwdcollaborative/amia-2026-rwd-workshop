# Notebooks

Open these in JupyterLab on the Workbench. They read from BigQuery.

| Notebook | Dataset | What it shows |
|----------|---------|---------------|
| `survival_and_a1c.ipynb` | `synpuf_workshop` | The workshop exercises: survival analysis and A1C by diabetes status. |
| `hex_density_map.ipynb` | `synpuf_workshop` + `staging/data/h3_res3.gpkg` | People per H3 hexagon via `location.h3_id`, mapped with geopandas. |

`synpuf_workshop` is built once per workspace by `staging/augment.py`; see
`staging/README.md`.

`bq_env.py` is imported by the notebooks (and by `staging/augment.py`). It
works around a Workbench image bug that can stop the BigQuery client from
importing.

## `survival_and_a1c.ipynb`

This notebook runs both analyses against `synpuf_workshop`:

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
`staging/augment.py`, because the A1C values were generated from that definition.
