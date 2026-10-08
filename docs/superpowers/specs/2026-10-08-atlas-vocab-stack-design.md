# ATLAS vocabulary-only stack — design

Date: 2026-10-08
Branch: `atlas`

## Goal

A docker-compose stack running OHDSI ATLAS + WebAPI with the OMOP standardized
vocabularies loaded and **no patient data**, so workshop attendees can search
concepts, browse hierarchies, and author/save/export concept sets. Built and
verified locally first; later deployed to a public server because the public
demo ATLAS is too slow for a workshop.

## Decisions

| Topic | Decision |
|---|---|
| Base | Own slim compose using official OHDSI images (not Broadsea as-is). Broadsea used only as a reference for config. |
| Auth | None. Open, anonymous; all concept sets in one shared pool (`DisabledSecurity`). |
| TLS / proxy | Out of scope for now. No Traefik, plain HTTP on localhost ports. |
| Vocab storage | Athena CSVs live outside the repo; path given via `VOCAB_DIR`, mounted read-only. |
| CPT4 | User runs Athena's CPT4 reconstitution in `VOCAB_DIR` before loading. Loader does not handle it. |
| Search | Solr-backed concept search, with WebAPI's SQL search as a working fallback. |
| Versions | Pinned: WebAPI 2.15.2, ATLAS 2.15.0, Postgres 16, Solr 8.11. No `:latest`. |

## Key finding: published WebAPI image has no Solr support

`ohdsi/webapi:2.15.2` is built with the default `MAVEN_PROFILE=webapi-docker`;
it contains no `webapi-solr` jars and `solr.endpoint` / `solr.query.prefix`
are unresolved placeholders. Setting `SOLR_ENDPOINT` on it does nothing.
We therefore build WebAPI from source at tag `v2.15.2` with
`MAVEN_PROFILE=webapi-docker,webapi-solr`, using WebAPI's own Dockerfile.

Solr must stay on 8.x: the vocab core is populated by the DataImportHandler
(JDBC pull from Postgres), which was removed in Solr 9.

## Layout

```
atlas/
  docker-compose.yml
  .env.example          # copy to .env (gitignored)
  webapi/Dockerfile     # builds WebAPI v2.15.2 with webapi-solr profile
  atlas/config-local.js # WebAPI URL, auth disabled
  solr/                 # core conf + data-config.xml (adapted from Broadsea)
  sql/                  # vocab DDL, indexes/constraints, source registration
  scripts/load-vocab.sh # loader entrypoint (runs inside vocab-loader container)
  README.md
```

`.gitignore` gains `atlas/.env`.

## Services

All on one compose network.

- **`db`** — `postgres:16`. One database, two schemas:
  - `webapi` — owned by WebAPI; created and migrated by its Flyway on boot.
  - `vocab` — Athena tables, created by the loader.
  Data in a named volume by default. `PGDATA_HOST_PATH` in `.env`, if set,
  switches to a bind mount (e.g. an external drive) for large vocab loads.
  Healthcheck: `pg_isready`.
- **`webapi`** — built from `webapi/Dockerfile`. Env-configured datasource
  pointing at `db` / schema `webapi`, `SECURITY_PROVIDER=DisabledSecurity`,
  CORS enabled, `SOLR_ENDPOINT=http://solr:8983/solr`. Exposed on
  `${WEBAPI_PORT:-8080}`. Depends on `db` healthy.
- **`atlas`** — `ohdsi/atlas:2.15.0`, static nginx. `config-local.js` is
  produced from env at container start (via the image's own templating if it
  supports `WEBAPI_URL`, otherwise our own envsubst entrypoint), with the
  browser-side WebAPI URL from `WEBAPI_PUBLIC_URL` (default
  `http://localhost:8080/WebAPI/`) so a later public host is a `.env` change.
  User auth disabled.
  Exposed on `${ATLAS_PORT:-8081}`.
- **`solr`** — `solr:8.11`, Postgres JDBC driver available on its classpath,
  core config mounted read-only, index data in a named volume. Exposed on
  `${SOLR_PORT:-8983}` for debugging.
- **`vocab-loader`** — one-shot, compose profile `load` (never starts with a
  plain `up`). `postgres:16` image for `psql`; mounts `${VOCAB_DIR}:/vocab:ro`
  plus `sql/` and `scripts/`. Run with
  `docker compose --profile load run --rm vocab-loader`.

## Loader flow (`scripts/load-vocab.sh`)

Fails fast (`set -euo pipefail`) and prints elapsed time per step.

1. **Validate** `/vocab` contains the Athena files: `CONCEPT`, `CONCEPT_ANCESTOR`,
   `CONCEPT_CLASS`, `CONCEPT_RELATIONSHIP`, `CONCEPT_SYNONYM`, `DOMAIN`,
   `DRUG_STRENGTH`, `RELATIONSHIP`, `VOCABULARY` (`.csv`). Missing file →
   clear error naming it.
2. **Recreate schema**: `DROP SCHEMA IF EXISTS vocab CASCADE`, then OMOP CDM
   v5.4 vocabulary-table DDL (no constraints/indexes yet). Idempotent; a new
   vocab version = new `VOCAB_DIR` + rerun.
3. **Bulk load** each table with
   `\copy vocab.<t> FROM '/vocab/<T>.csv' WITH (FORMAT csv, DELIMITER E'\t', QUOTE E'\b', HEADER)`.
   Date columns are `YYYYMMDD`, which Postgres parses as dates directly.
4. **Constraints & indexes** from the OMOP v5.4 Postgres scripts (PKs + the
   standard concept/relationship/ancestor indexes), then `ANALYZE`.
5. **Register source** (requires WebAPI to have completed Flyway migration;
   loader waits for `webapi.source` to exist, with timeout): upsert one row in
   `webapi.source` (key `VOCAB`, name "OMOP Vocabulary", JDBC URL to `db`,
   dialect `postgresql`) and one `webapi.source_daimon` row (daimon type 1 =
   Vocabulary, table qualifier `vocab`, priority 1). Then call
   `GET /WebAPI/source/refresh` so no restart is needed.
6. **Solr index** (skipped with a notice if Solr is unreachable): read
   `vocabulary_version` from `vocab.vocabulary WHERE vocabulary_id = 'None'`,
   create a core named with that string (spaces → underscores, matching
   WebAPI's lookup), run DataImportHandler `full-import` for entity `concept`,
   poll status until complete.

The ordering requirement (WebAPI up before step 5) is satisfied by running the
loader against an already `up` stack; the README states this.

## Dev vocab subset

The loader is bundle-agnostic; the subset is a smaller Athena download. Dev
recommendation: **SNOMED, ICD10CM, RxNorm** (Athena adds its base vocabularies
automatically). Covers hierarchy browsing, source→standard mapping, and drug
concept sets. Full vocab (~15–25 GB loaded) is for the server or an external
`PGDATA_HOST_PATH`.

## Verification (manual, in order)

Each layer is checked before the next is built.

1. **Base stack, no vocab**: `docker compose up -d`; `curl :8080/WebAPI/info`
   returns version JSON; ATLAS loads at `:8081` with no config errors in the
   browser console.
2. **Vocab + source, Solr off**: loader completes; `/WebAPI/source/sources`
   lists `VOCAB`; in ATLAS, concept search for "type 2 diabetes" returns SNOMED
   201826; concept detail shows hierarchy and related concepts; create a
   concept set with descendants, save it, reload page, it persists; export
   works.
3. **Solr on**: WebAPI log shows Solr search provider active for the core;
   same search returns results via Solr; compare response time vs. step 2.

Open question resolved during step 2: whether ATLAS is fully happy with a
source that has only a Vocabulary daimon (record-count columns should simply
be empty). If not, fallback is adding an empty results schema + Results
daimon — still no patient data.

## Out of scope (for now)

- HTTPS, reverse proxy, domain names
- Authentication / per-user concept sets
- Server provisioning and deployment (later task; this design keeps everything
  in `.env` + one folder so it ports by copy)
- Patient data, CDM, Achilles/results, cohort generation
- Automated tests beyond the manual verification above
