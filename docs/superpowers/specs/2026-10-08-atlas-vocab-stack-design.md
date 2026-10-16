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
  .env.example              # copy to .env (gitignored)
  load-vocab.sh             # host-side loader entrypoint (orchestrates the steps below)
  scripts/load-vocab-db.sh  # runs inside the vocab-loader container: DDL, \copy, indexes, source
  sql/                      # vocab DDL, indexes/constraints, source registration
  solr/Dockerfile           # solr:8.11.4 + Postgres JDBC driver + omop_vocab configset
  solr/conf/                # configset (vendored from Broadsea @ 7ca896a, DIH datasource tuned)
  tests/smoke.sh            # layered smoke tests (base | vocab | solr)
  tests/test.env            # isolated test project: own name, ports, volumes
  tests/fixtures/vocab-mini/  # tiny synthetic Athena-format bundle
  README.md
```

WebAPI is built by compose directly from `https://github.com/OHDSI/WebAPI.git#v2.15.2`
with build arg `MAVEN_PROFILE=webapi-docker,webapi-solr` (no Dockerfile in this
repo). ATLAS needs no config file: the `ohdsi/atlas` image renders
`config-local.js` from env vars (`WEBAPI_URL`, `ATLAS_*`) at container start.

`.gitignore` gains `atlas/.env`.

## Services

All on one compose network.

- **`db`** — `postgres:16`. One database, two schemas:
  - `webapi` — owned by WebAPI; created and migrated by its Flyway on boot.
  - `vocab` — Athena tables, created by the loader.
  Data in a named volume by default. `PGDATA_HOST_PATH` in `.env`, if set,
  switches to a bind mount (e.g. an external drive) for large vocab loads.
  Healthcheck: `pg_isready`. Port bound to 127.0.0.1 only.
- **`webapi`** — built from the WebAPI git tag (see Layout). Env-configured datasource
  pointing at `db` / schema `webapi`, `SECURITY_PROVIDER=DisabledSecurity`,
  CORS enabled, `SOLR_ENDPOINT=http://solr:8983/solr`. Exposed on
  `${WEBAPI_PORT:-8080}`. Depends on `db` healthy.
- **`atlas`** — `ohdsi/atlas:2.15.0`, static nginx. Configured purely by env:
  `WEBAPI_URL=${WEBAPI_PUBLIC_URL}` (browser-side URL, default
  `http://localhost:8080/WebAPI/`, so a later public host is a `.env` change),
  security provider/permissions/T&Cs/person-count off.
  Exposed on `${ATLAS_PORT:-8081}`, app at `/atlas/`.
- **`solr`** — built from `solr/Dockerfile` (`solr:8.11.4` + Postgres JDBC
  driver + `omop_vocab` configset). Index data in a named volume. Port bound to
  127.0.0.1 for debugging only. WebAPI `depends_on` Solr healthy so its startup
  core scan can reach Solr.
- **`vocab-loader`** — one-shot, compose profile `load` (never starts with a
  plain `up`). `postgres:16` image for `psql`; mounts `${VOCAB_DIR}:/vocab:ro`
  plus `sql/` and `scripts/`. Run with
  `docker compose --profile load run --rm vocab-loader`.

## Loader flow

Host-side `atlas/load-vocab.sh` (needs only docker + curl on the host). Fails
fast and prints elapsed time per step. `ENV_FILE` selects the env file
(default `.env`; tests use `tests/test.env`).

1. **Validate** (host side, before anything is touched) that `VOCAB_DIR`
   contains `CONCEPT`, `CONCEPT_ANCESTOR`, `CONCEPT_CLASS`,
   `CONCEPT_RELATIONSHIP`, `CONCEPT_SYNONYM`, `DOMAIN`, `DRUG_STRENGTH`,
   `RELATIONSHIP`, `VOCABULARY` (`.csv`). Missing → error naming each file;
   existing vocab left intact.
2. **Ensure stack is up**: `docker compose up -d --wait` (WebAPI healthcheck
   implies its Flyway migration has created `webapi.source`).
3. **Load into Postgres** via the `vocab-loader` container
   (`scripts/load-vocab-db.sh`):
   a. `DROP SCHEMA IF EXISTS vocab CASCADE`, OMOP CDM v5.4 vocab DDL.
   b. `\copy vocab.<t> FROM '/vocab/<T>.csv' WITH (FORMAT csv, DELIMITER E'\t', QUOTE E'\b', HEADER)`.
   c. PKs + standard OMOP v5.4 indexes (no `CLUSTER`: it doubles disk use), `ANALYZE`.
   d. Upsert source `VOCAB` in `webapi.source` and one `webapi.source_daimon`
      row (daimon type 1 = Vocabulary, qualifier `vocab`, priority 1), using
      the `source_key_unique` and `un_source_daimon` constraints.
4. **Solr index** (skipped with a notice if the `solr` service is not
   running): read `vocabulary_version` where `vocabulary_id = 'None'`, spaces →
   underscores = core name (exactly how WebAPI derives its lookup key). Create
   the core from the `omop_vocab` configset if absent, run DIH `full-import`,
   poll until done; fail on "Indexing failed".
5. **Restart WebAPI** and wait for healthy. Required: WebAPI's Solr provider
   reads the core list only once, at startup. Restart also clears source and
   vocabulary-info caches.

## Dev vocab subset

The loader is bundle-agnostic; the subset is a smaller Athena download. Dev
recommendation: **SNOMED, ICD10CM, RxNorm** (Athena adds its base vocabularies
automatically). Covers hierarchy browsing, source→standard mapping, and drug
concept sets. Full vocab (~15–25 GB loaded) is for the server or an external
`PGDATA_HOST_PATH`.

## Verification

**Automated smoke tests** (`atlas/tests/smoke.sh`) run against an isolated
compose project (`tests/test.env`: own project name, ports, volumes) loaded
with a tiny synthetic Athena-format fixture, so they never touch the dev
stack's data and need no Athena download. Layers:

1. `base`: WebAPI `/info` responds; ATLAS serves `config-local.js` containing
   the configured WebAPI URL.
2. `vocab`: source `VOCAB` registered; DB-backed search finds 201826; a name
   containing `"` and `\` round-trips intact; descendants resolve through
   `concept_ancestor`; a concept set can be created, given items, and read
   back; loader rerun is idempotent (one source row, concept set survives);
   missing file → non-zero exit naming it, existing vocab untouched.
3. `solr`: core named from the vocab version exists and is populated; WebAPI
   search returns results; stopping Solr makes WebAPI search return empty
   (proves WebAPI is actually using Solr), restarting restores it.

**Manual check with a real Athena dev subset** in the ATLAS UI: search "type 2
diabetes", open 201826, browse hierarchy, build/save/reload/export a concept
set with descendants.

Open question resolved by the `vocab` layer: whether WebAPI/ATLAS accept a
source with only a Vocabulary daimon. If not, fallback is an empty results
schema + Results daimon — still no patient data.

**Known behavior to document:** if Solr goes down while WebAPI is running,
WebAPI search silently returns no results (it catches the error) rather than
falling back to SQL. `restart: unless-stopped` on Solr; README troubleshooting
notes the symptom.

## Out of scope (for now)

- HTTPS, reverse proxy, domain names
- Authentication / per-user concept sets
- Server provisioning and deployment (later task; this design keeps everything
  in `.env` + one folder so it ports by copy)
- Patient data, CDM, Achilles/results, cohort generation
- CI
