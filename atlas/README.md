# Workshop ATLAS (vocabulary only)

A docker-compose stack running OHDSI ATLAS with the OMOP standardized
vocabularies and **no patient data**. Attendees can search concepts, browse
hierarchies, and build, save and export concept sets. Everyone shares one
anonymous workspace (no logins).

| Service | Purpose | URL |
|---|---|---|
| atlas | ATLAS UI | http://localhost:8081/atlas/ |
| webapi | OHDSI WebAPI 2.15.2 (built with Solr support) | http://localhost:8080/WebAPI/info |
| solr | Fast concept search | http://127.0.0.1:8983/solr/ |
| db | Postgres 16: `webapi` + `vocab` schemas | 127.0.0.1:5432 |

## Quick start

1. Download vocabularies from https://athena.ohdsi.org (Download tab). For
   development pick a small set, e.g. **SNOMED, ICD10CM, RxNorm**. Unzip
   somewhere **outside this repo**. If you included CPT4, run Athena's
   `cpt.sh` / `cpt.bat` in that folder first (needs a UMLS API key).
2. `cp .env.example .env`, set `POSTGRES_PASSWORD`, and set `VOCAB_DIR` to the
   unzipped folder.
3. `./load-vocab.sh`. The first run builds WebAPI from source (10-20 min),
   then loads the vocabulary, builds the Solr index and restarts WebAPI.
4. Open http://localhost:8081/atlas/.

To load a different bundle later: `./load-vocab.sh /path/to/other/bundle`.
The vocab schema is replaced, and saved concept sets are kept.

Day to day: `docker compose up -d` / `docker compose down`. Data lives in the
`pgdata` and `solrdata` volumes. `docker compose down -v` deletes them
(including saved concept sets).

The Solr configset is copied into the `solrdata` volume at container start by
`solr/install-configset.sh`, so edits under `solr/` (Dockerfile or conf) take
effect after a rebuild and restart:
`docker compose build solr && docker compose up -d solr`. A plain `up` will
not rebuild the image.

## Sizing

| Bundle | Disk (Postgres) | Load | Solr index |
|---|---|---|---|
| Dev subset (SNOMED, ICD10CM, RxNorm) - not yet measured | TBD (run ./load-vocab.sh and record) | TBD (run ./load-vocab.sh and record) | TBD (run ./load-vocab.sh and record) |
| Full Athena download | ~15-25 GB (estimate) | - | - |

For a full bundle on a small laptop disk, set `PGDATA_HOST_PATH` in `.env`
to a folder on an external drive, and raise `SOLR_HEAP` to `2g`.

## Deploying to a server

Copy this folder, create `.env` with `WEBAPI_PUBLIC_URL=http://<host>:8080/WebAPI/`,
put the vocabulary on the server, and run `./load-vocab.sh`. Ports 8080
(WebAPI) and 8081 (ATLAS) must be reachable; Postgres and Solr stay on
localhost. HTTPS is not set up yet.

## Troubleshooting

- **Search returns nothing, but the concept set pages work.** Solr is probably
  down. WebAPI does not fall back to SQL once a Solr core exists. Run
  `docker compose ps solr`, then `docker compose up -d solr`.
- **Search is slow / Solr index missing after loading a new vocab version.**
  Rerun `./load-vocab.sh`. Indexing names the core after the vocab version,
  and WebAPI only notices new cores when it restarts (the loader restarts it).
- **A load failed partway (e.g. a malformed CSV).** A load replaces the
  `vocab` schema in place, so until `./load-vocab.sh` completes with a good
  bundle, search and concept browsing are broken. Load vocabularies before a
  session, not during one.
- **Changed something under `solr/`.** Run
  `docker compose build solr && docker compose up -d solr`; plain `up` won't
  rebuild the image.
- **WebAPI takes minutes to become healthy on first start.** That's expected:
  it runs ~300 database migrations. `docker compose logs -f webapi`.
- **Disk full.** WebAPI's build cache is large. `docker builder prune` after
  the first successful build.

## Tests

`tests/smoke.sh all` runs layered checks (base stack, vocab loading, Solr)
against a separate compose project (`tests/test.env`, ports 18080/18081)
loaded with a tiny synthetic vocabulary in `tests/fixtures/vocab-mini`. It
never touches your dev data. `tests/smoke.sh down` removes it.

## Credits

Solr configset adapted from [OHDSI Broadsea](https://github.com/OHDSI/Broadsea)
(Apache-2.0).
