# SynPUF data findings

What we learned about the workshop's `synpuf_omop` BigQuery dataset while porting
the exercises from the earlier (Palantir Foundry) workshop, and the decisions
that followed. Most numbers come from `staging/diagnostics.py`, run 2026-10-16
on workspace project `wb-glossy-cherry-7026` (dataset `synpuf_omop`, 25 tables,
`us-central1`). Re-check them if the dataset is reloaded.

## The data

- **Time span.** Clinical data runs from 2007-11 to 2010-12, so no one can be
  followed for five years. The five-year censoring step never triggers.
- **Measurements carry no values.** None of the 2.9M `measurement` rows has a
  `value_as_number`. The A1C values in the exercise have to be synthesized.
- **A1C codes.** CPT 83036 maps to concept 2212392 (84,877 rows). CPT 83037 (a
  home-device A1C test) maps to 2212393 (1,471 rows).
  - 2212392 is **non-standard** in the current vocabulary. Its "Maps to" target
    is 4184637 *Hemoglobin A1c measurement*.
  - 2212393 is standard and maps to itself.
  - The original A1C concept set doesn't include 2212392. Its only match is
    2212393, which the set marks as *excluded*. So honoring the exclusions finds
    zero rows, and ignoring them (the original's behavior) finds only the
    home-test rows.
- **Pancreatic cancer.** The original seed, 42872399 *Primary adenocarcinoma of
  pancreas*, matches no rows. Real pancreatic cancer is present through ICD-9
  157.x, mapped to 199754 *Primary malignant neoplasm of pancreas* and its
  siblings (about 3,600 people under 4180793 *Malignant neoplasm of pancreas*).
  Synthetic 42872399 rows sit underneath both of those broader concepts.
- **Diabetes.**
  - Type 2 diabetes (201826) has only 16 descendants, and only two of them
    occur in the data.
  - The "disorder due to type 2 diabetes" complication concepts (e.g. 443732,
    376065) are standard and valid. They simply aren't descendants of 201826 in
    the current SNOMED hierarchy.
  - Separately, about 7.5% of `condition_occurrence` rows (~954k) use concepts
    that are now deprecated or upgraded.
- **Deaths look independent of everything else.** There are 4,635 death rows
  (2008–2010), one per person, with no clinical activity after death. Age has
  no effect on mortality (75+: 4.7% vs. 4.6%). Every condition checked
  *lowers* it, for example:

  | Condition | Died, with | Died, without |
  |-----------|-----------:|--------------:|
  | Diabetes | 3.5% | 7.3% |
  | CHF | 3.0% | 6.1% |
  | Pancreatic cancer | 2.4% | 4.7% |
  | 200+ condition rows | 1.8% | 5.6% |

  The likely cause is that early deaths have less time to accumulate claims, a
  time-at-risk artifact. About half of decedents have type 2 diabetes.
- **Location.** The `location` table has 3,185 rows with only state and county
  codes. Zip, city and address are empty. Every person has a valid
  `location_id`. The codes are SSA state+county codes, not FIPS (e.g. county
  `54520`, source value `54-520`). Each location is a county, which is why there
  are only ~3,185 of them: about 3,140 US counties plus an "unknown county"
  (`x999`) per state. State `54` isn't in the NBER SSA crosswalk; it is probably
  SynPUF's "other" code, but that's unconfirmed. There is no
  `location_history` table (it first appears in CDM 5.4).
- **IDs.** The original +16,000,000 offset for new `measurement_id` values would
  collide with 52 existing IDs.

## The Workbench environment

- **Dataset creation.** Users can't create BigQuery datasets through the API
  (`bigquery.datasets.create` is denied). Create datasets as Workbench resources
  instead (UI or `wb resource create bq-dataset`).
- **No `WORKSPACE_CDR`.** The workbench doesn't set it. Notebooks define `CDR`
  themselves, following the All of Us convention.
- **Query access.** `pandas.read_gbq` works but is deprecated (pandas 3 removes
  it). Use `pandas_gbq.read_gbq` instead. DATE columns come back as `dbdate` and
  need `pd.to_datetime`.
- **Broken BigQuery import on fresh VMs.** The JupyterLab image ships
  `google-cloud-bigquery` 3.45, which needs `google-api-core` >= 2.28. The
  image build then pip-installs `dsub`, which pins `google-api-core` <= 2.25.1,
  so `from google.cloud import bigquery` fails out of the box.
  `notebooks/bq_env.py` restores `google-api-core[grpc]==2.38.0` when the
  import fails. That breaks only `dsub`.

## Decisions

- **Keep the original's injected demo.** The original exercises ran on an
  OMOP-converted Synthea dataset. Because SynPUF's deaths carry no clinical
  signal, we keep the colleagues' injected pancreatic-cancer cohort rather than
  redesigning the exercise.
- **`staging/augment.py` builds `synpuf_workshop`.** It's a separate Workbench
  dataset in which `condition_occurrence` and `measurement` are modified tables
  and everything else is a view onto `synpuf_omop`.
- **Pancreatic cancer.** 10% of decedents get a 42872399 diagnosis dated
  `1 + Exp(mean 300)` days before death. About 10% of the pancreatic-cancer
  group comes from living people instead, with the diagnosis dated before their
  last activity, so they're censored. This replaces the original's "filter"
  step, which duplicated death rows. The death table is untouched. The first
  build produced 488 decedents and 58 censored patients.
- **A1C.** Rows are recoded to their "Maps to" concept, 4184637, keeping the CPT
  code as the source concept. 2212393 rows are recoded there too. Values are
  drawn by diabetes status on the test date: means 7.5 and 5.0.
- **Diabetes definition (provisional).** Seeds 442793 *Complication due to
  diabetes mellitus* and 4008576 *Diabetes mellitus without complication* came
  from an ATLAS search, not from the colleagues' real concept sets. On their own
  they found only 65% of people with an ICD-9 250.x code, so 201826 (type 2) and
  201254 (type 1) were added. The definition appears in both
  `staging/augment.py` (`DIABETES_SEEDS`) and section 2.2 of
  `notebooks/survival_and_a1c.ipynb`, and the two must stay in sync.
- **Hexagons.** `augment.py` adds `location.h3_id`: SSA code → county FIPS (NBER
  2011 crosswalk) → the county's H3 resolution-3 hexagon
  (`staging/data/h3res3_countyfips_cw.csv`, one hexagon per county). The first
  build (2026-10-22) placed 96,781 of 100,000 people in 643 hexagons. The misses
  came from the hexagon crosswalk using *current* county FIPS while SynPUF is
  ~2010:

  | Unmatched | People | Cause |
  |---|---:|---|
  | State `54` | 1,456 | SynPUF's "other" code; not fixable |
  | Connecticut | 1,291 | Counties replaced by planning regions in 2022 |
  | Honolulu, HI | 375 | `15003` absent from the hexagon crosswalk |
  | AK, SD, VA | ~45 | Wade Hampton → Kusilvak, Shannon → Oglala Lakota, Bedford city merged into Bedford County |
  | Various | ~50 | SSA codes in neither crosswalk (synthetic noise) |

  `staging/data/county_h3_supplement.csv` now covers the CT, Honolulu and
  rename/merge rows. CT's 9 planning regions fall in only 3 hexagons, so each
  old county maps to one of them; the county-to-region pairing is approximate.
  With it, 98,470 of 100,000 people (98.5%) are placed, in 639 hexagons.
