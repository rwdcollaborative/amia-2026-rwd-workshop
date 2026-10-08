# ATLAS Vocabulary-Only Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A docker-compose stack (Postgres, WebAPI, ATLAS, Solr) with OMOP vocabularies and no patient data, so workshop attendees can search concepts and author concept sets.

**Architecture:** Own slim compose in `atlas/`. WebAPI is built from its v2.15.2 git tag with the `webapi-solr` Maven profile (the published image lacks Solr support). A host-side `load-vocab.sh` validates an Athena bundle, loads it into a `vocab` schema through a one-shot `psql` container, registers it as a vocabulary-only WebAPI source, builds a Solr core named after the vocab version, and restarts WebAPI (which reads Solr cores only at startup). Layered bash smoke tests run against an isolated compose project loaded with a tiny synthetic fixture.

**Tech Stack:** Docker Compose v2, Postgres 16, OHDSI WebAPI 2.15.2 / ATLAS 2.15.0, Solr 8.11.4 (DataImportHandler), bash, curl; python3 (tests only, for JSON parsing).

**Spec:** `docs/superpowers/specs/2026-10-08-atlas-vocab-stack-design.md`

## Global Constraints

- Pinned versions, no `:latest`: WebAPI `v2.15.2` (git tag), ATLAS `ohdsi/atlas:2.15.0`, `postgres:16`, `solr:8.11.4`, Postgres JDBC `42.7.4`, Broadsea solr conf vendored from commit `7ca896ae7d2c33f5197a7b47aec0a29d1e897d2f`.
- WebAPI build arg: `MAVEN_PROFILE=webapi-docker,webapi-solr`.
- No patient data, no CDM clinical tables, no results schema (unless the vocab-only-daimon fallback in Task 2 is triggered).
- Vocabulary CSVs never enter the repo or an image; they are bind-mounted read-only from `VOCAB_DIR`.
- Security: `DisabledSecurity`, open/anonymous. No Traefik, no TLS.
- `db` and `solr` host ports bind to `127.0.0.1` only; `webapi` and `atlas` bind on all interfaces.
- Host requirements for running the stack: docker (compose v2 with `--wait`) and curl. python3 only for `tests/smoke.sh`.
- Commit messages use the repo's `area: summary` style (here `atlas: ...`) and end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Disk is tight on the dev laptop (~27 GB free): never load the full vocabulary locally during this plan; use the fixture and a small Athena subset.

## Review Focus

1. **Athena text containing `"` or `\`** (real concept names have both) — must load byte-for-byte; a CSV quoting mistake either aborts `\copy` or silently mangles names. Pinned by the `vocab` smoke layer (concept 9000001).
2. **Re-running the loader** (same or new vocab version) — must not duplicate the source row, error on existing rows, or lose concept sets users already saved. Pinned by the `vocab` layer rerun check.
3. **Incomplete or wrong `VOCAB_DIR`** — must fail before touching the database, name every missing file, and leave the currently loaded vocab working. Pinned by the `vocab` layer missing-file check.
4. **Solr down while WebAPI runs** — WebAPI silently returns empty search results (it catches the error, no SQL fallback). The `solr` layer pins this behavior so it's known; README troubleshooting documents the symptom.
5. **Full-scale vocab (millions of concepts)** — Solr DIH must stream rows rather than buffer them, and load/index time must be known for server sizing. Not reproducible with the fixture; owned by Task 4's manual run on a real Athena subset (watch for Solr OOM, record timings).

---

## File Structure

| Path | Responsibility |
|---|---|
| `.gitignore` | Ignore `atlas/.env` |
| `atlas/docker-compose.yml` | All services, volumes, healthchecks |
| `atlas/.env.example` | Documented config template |
| `atlas/load-vocab.sh` | Host-side loader orchestration (validate → up → load → solr → restart/refresh) |
| `atlas/scripts/load-vocab-db.sh` | In-container: DDL, `\copy`, indexes, source registration |
| `atlas/sql/vocab_ddl.sql` | Drop/create `vocab` schema and tables |
| `atlas/sql/vocab_indexes.sql` | PKs, indexes, ANALYZE |
| `atlas/sql/register_source.sql` | Upsert WebAPI source + vocabulary daimon |
| `atlas/solr/Dockerfile` | Solr image with JDBC driver and configset |
| `atlas/solr/conf/` | `omop_vocab` configset (vendored + `data-config.xml`) |
| `atlas/tests/smoke.sh` | Layered smoke tests |
| `atlas/tests/test.env` | Isolated test project config |
| `atlas/tests/fixtures/vocab-mini/*.csv` | Synthetic Athena-format bundle |
| `atlas/README.md` | Usage, Athena download, troubleshooting |

---

### Task 1: Base stack (db + WebAPI with Solr profile + ATLAS) with `base` smoke layer

**Files:**
- Create: `.gitignore`
- Create: `atlas/docker-compose.yml`
- Create: `atlas/.env.example`
- Create: `atlas/tests/test.env`
- Create: `atlas/tests/smoke.sh`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Compose services `db`, `webapi`, `atlas` (Task 2 adds `vocab-loader`, Task 3 adds `solr`).
  - Env vars used by all later tasks: `COMPOSE_PROJECT_NAME`, `VOCAB_DIR`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `WEBAPI_PORT`, `ATLAS_PORT`, `SOLR_PORT`, `DB_PORT`, `WEBAPI_PUBLIC_URL`, `ATLAS_INSTANCE_NAME`, `PGDATA_HOST_PATH`, `SOLR_HEAP`.
  - `tests/smoke.sh <layer>` harness with helpers `dc`, `pass`, `fail`, `assert_contains`, `assert_eq`, `psql_q`, `json`, and layer functions `layer_base` (Task 2 adds `layer_vocab`, Task 3 adds `layer_solr`).
  - Docker image tag `atlas-workshop/webapi:2.15.2-solr`.

- [ ] **Step 1: Create the test env file and the smoke harness with the `base` layer (the failing test)**

`atlas/tests/test.env`:

```bash
# Isolated compose project for tests/smoke.sh: own name, ports and volumes,
# so tests never touch the dev stack's data.
COMPOSE_PROJECT_NAME=atlas-test
VOCAB_DIR=./tests/fixtures/vocab-mini
POSTGRES_DB=ohdsi
POSTGRES_USER=ohdsi
POSTGRES_PASSWORD=test
WEBAPI_PORT=18080
ATLAS_PORT=18081
SOLR_PORT=18983
DB_PORT=15432
WEBAPI_PUBLIC_URL=http://localhost:18080/WebAPI/
ATLAS_INSTANCE_NAME="ATLAS test"
SOLR_HEAP=512m
```

`atlas/tests/smoke.sh`:

```bash
#!/usr/bin/env bash
# Layered smoke tests for the ATLAS stack, run against the isolated compose
# project in tests/test.env (own name, ports, volumes).
#
# Usage: tests/smoke.sh base|vocab|solr|all
#        tests/smoke.sh down      # remove the test project and its volumes
set -uo pipefail
cd "$(dirname "$0")/.."

export ENV_FILE=tests/test.env
set -a; . "./$ENV_FILE"; set +a

WEBAPI="http://localhost:${WEBAPI_PORT}/WebAPI"
FIXTURE=./tests/fixtures/vocab-mini
FAILS=0

dc()   { docker compose --env-file "$ENV_FILE" "$@"; }
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }
assert_contains() { # name haystack needle
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1 (missing: $3)"; fi
}
assert_eq() { # name actual expected
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (got: $2, want: $3)"; fi
}
psql_q() { dc exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }
json()   { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

layer_base() {
  echo "== base"
  if dc up -d --wait; then pass "stack starts healthy"; else fail "stack starts healthy"; return; fi
  assert_contains "WebAPI /info reports a version" \
    "$(curl -fsS "$WEBAPI/info")" '"version"'
  assert_contains "ATLAS config points at WebAPI" \
    "$(curl -fsS "http://localhost:${ATLAS_PORT}/atlas/js/config-local.js")" "$WEBAPI_PUBLIC_URL"
}

case "${1:-}" in
  base) layer_base ;;
  all)  layer_base ;;
  down) dc --profile load down -v; exit 0 ;;
  *)    echo "usage: $0 base|vocab|solr|all|down" >&2; exit 2 ;;
esac

echo
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
```

Run: `chmod +x atlas/tests/smoke.sh`

- [ ] **Step 2: Run it to verify it fails**

Run: `atlas/tests/smoke.sh base`
Expected: `FAIL stack starts healthy` (compose reports no configuration file), exit 1.

- [ ] **Step 3: Write the compose file, env template and gitignore**

`.gitignore` (repo root, new file):

```
atlas/.env
```

`atlas/.env.example`:

```bash
# Copy to .env and edit. .env is gitignored; never commit it.
COMPOSE_PROJECT_NAME=atlas

# Unzipped Athena vocabulary bundle (CONCEPT.csv, CONCEPT_ANCESTOR.csv, ...).
# Lives outside this repo. Absolute path, or relative to this directory.
# Can also be given on the command line: ./load-vocab.sh /path/to/bundle
VOCAB_DIR=

POSTGRES_DB=ohdsi
POSTGRES_USER=ohdsi
POSTGRES_PASSWORD=change-me

# Host ports. DB and Solr are bound to 127.0.0.1 only.
WEBAPI_PORT=8080
ATLAS_PORT=8081
SOLR_PORT=8983
DB_PORT=5432

# URL the *browser* uses to reach WebAPI. Change when deploying to a server.
WEBAPI_PUBLIC_URL=http://localhost:8080/WebAPI/
ATLAS_INSTANCE_NAME="Workshop ATLAS"

# Where Postgres keeps its data: a named Docker volume by default. Set to a
# host path (e.g. on an external drive) for a full-vocabulary load.
#PGDATA_HOST_PATH=/Volumes/External/atlas-pgdata

# Solr JVM heap. 1g is fine for a vocab subset; use 2g+ for the full vocab.
SOLR_HEAP=1g
```

`atlas/docker-compose.yml`:

```yaml
# Vocabulary-only OHDSI ATLAS stack: no patient data. See README.md.

services:
  db:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-ohdsi}
      POSTGRES_USER: ${POSTGRES_USER:-ohdsi}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
    # Bulk-load friendly, but modest enough for an 8 GB Docker VM.
    command: >-
      postgres -c shared_buffers=512MB -c maintenance_work_mem=512MB
      -c max_wal_size=4GB -c work_mem=32MB
    shm_size: 1g
    volumes:
      - ${PGDATA_HOST_PATH:-pgdata}:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:${DB_PORT:-5432}:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 20

  webapi:
    # The published ohdsi/webapi image is built without the webapi-solr
    # profile, so Solr search silently does nothing. Build it ourselves.
    build:
      context: https://github.com/OHDSI/WebAPI.git#v2.15.2
      args:
        MAVEN_PROFILE: webapi-docker,webapi-solr
    image: atlas-workshop/webapi:2.15.2-solr
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATASOURCE_DRIVERCLASSNAME: org.postgresql.Driver
      DATASOURCE_URL: jdbc:postgresql://db:5432/${POSTGRES_DB:-ohdsi}
      DATASOURCE_USERNAME: ${POSTGRES_USER:-ohdsi}
      DATASOURCE_PASSWORD: ${POSTGRES_PASSWORD}
      DATASOURCE_OHDSI_SCHEMA: webapi
      SPRING_JPA_PROPERTIES_HIBERNATE_DIALECT: org.hibernate.dialect.PostgreSQLDialect
      SPRING_JPA_PROPERTIES_HIBERNATE_DEFAULT_SCHEMA: webapi
      SPRING_BATCH_REPOSITORY_TABLEPREFIX: webapi.BATCH_
      FLYWAY_DATASOURCE_DRIVERCLASSNAME: org.postgresql.Driver
      FLYWAY_DATASOURCE_URL: jdbc:postgresql://db:5432/${POSTGRES_DB:-ohdsi}
      FLYWAY_DATASOURCE_USERNAME: ${POSTGRES_USER:-ohdsi}
      FLYWAY_DATASOURCE_PASSWORD: ${POSTGRES_PASSWORD}
      FLYWAY_LOCATIONS: classpath:db/migration/postgresql
      FLYWAY_PLACEHOLDERS_OHDSISCHEMA: webapi
      FLYWAY_SCHEMAS: webapi
      FLYWAY_TABLE: schema_history
      SECURITY_PROVIDER: DisabledSecurity
      SECURITY_CORS_ENABLED: "true"
      SECURITY_ORIGIN: "*"
      SOLR_ENDPOINT: http://solr:8983/solr
    ports:
      - "${WEBAPI_PORT:-8080}:8080"
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:8080/WebAPI/info >/dev/null"]
      interval: 10s
      timeout: 5s
      start_period: 300s
      retries: 30

  atlas:
    image: ohdsi/atlas:2.15.0
    restart: unless-stopped
    # The image renders config-local.js from these at startup.
    environment:
      WEBAPI_URL: ${WEBAPI_PUBLIC_URL:-http://localhost:8080/WebAPI/}
      ATLAS_INSTANCE_NAME: ${ATLAS_INSTANCE_NAME:-Workshop ATLAS}
      ATLAS_SECURITY_PROVIDER_ENABLED: "false"
      ATLAS_ENABLE_PERMISSIONS_MGMT: "false"
      ATLAS_ENABLE_TANDCS: "false"
      ATLAS_ENABLE_PERSONCOUNT: "false"
      ATLAS_DISABLE_BROWSER_CHECK: "true"
    ports:
      - "${ATLAS_PORT:-8081}:8080"

volumes:
  pgdata:
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `atlas/tests/smoke.sh base`
Expected: the first run builds WebAPI from source (10–20 min; Maven downloads dependencies) and waits for Flyway migrations. Then:

```
== base
  ok   stack starts healthy
  ok   WebAPI /info reports a version
  ok   ATLAS config points at WebAPI

all checks passed
```

If the WebAPI build fails, read the Maven error. The most likely cause is `repo.ohdsi.org` being unreachable for `org.ohdsi:webapi-solr:1.0.0`. Retry before changing anything. If ATLAS returns 404 on `/atlas/js/config-local.js`, check `docker compose --env-file tests/test.env logs atlas` for the nginx listen port (the plan assumes 8080 inside the container).

Then confirm Solr support is compiled in (it should not be in the published image):

Run: `docker run --rm --entrypoint sh atlas-workshop/webapi:2.15.2-solr -c 'ls WEB-INF/lib | grep -c -i solr'`
Expected: a number ≥ 1.

- [ ] **Step 5: Commit**

```bash
git add .gitignore atlas/docker-compose.yml atlas/.env.example atlas/tests/test.env atlas/tests/smoke.sh
git commit -m "atlas: base compose stack (postgres, WebAPI w/ solr profile, ATLAS) + smoke test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Vocabulary loader + source registration with `vocab` smoke layer

**Files:**
- Create: `atlas/tests/fixtures/vocab-mini/{CONCEPT,VOCABULARY,DOMAIN,CONCEPT_CLASS,RELATIONSHIP,CONCEPT_RELATIONSHIP,CONCEPT_ANCESTOR,CONCEPT_SYNONYM,DRUG_STRENGTH}.csv`
- Create: `atlas/sql/vocab_ddl.sql`, `atlas/sql/vocab_indexes.sql`, `atlas/sql/register_source.sql`
- Create: `atlas/scripts/load-vocab-db.sh`
- Create: `atlas/load-vocab.sh`
- Modify: `atlas/docker-compose.yml` (add `vocab-loader` service)
- Modify: `atlas/tests/smoke.sh` (add `layer_vocab`, wire into `case`)

**Interfaces:**
- Consumes: Task 1 services and env vars; `smoke.sh` helpers.
- Produces:
  - `./load-vocab.sh [VOCAB_DIR]`: run from `atlas/`. Honors `ENV_FILE` (default `.env`). The positional arg overrides `VOCAB_DIR` from the env file. Exit 0 on success. Non-zero with `ERROR: ...` on stderr on failure.
  - Helper functions in `load-vocab.sh` that Task 3 extends: `die`, `dc`, `step <label> <cmd...>`, `wait_webapi`, `solr_running` (Task 3 adds `build_solr_index`).
  - Postgres schema `vocab` with the 9 OMOP vocab tables. WebAPI source key `VOCAB`.
  - Fixture vocab version `v5.0 FIXTURE-01-JAN-26` (Solr core name `v5.0_FIXTURE-01-JAN-26`). The fixture has 6 concepts.

- [ ] **Step 1: Create the synthetic fixture bundle**

These files are **tab-separated** with a header row and dates as `YYYYMMDD`, exactly like Athena. Create them with `printf` so the tabs are real:

```bash
F=atlas/tests/fixtures/vocab-mini; mkdir -p $F

printf '%s\n' \
'concept_id	concept_name	domain_id	vocabulary_id	concept_class_id	standard_concept	concept_code	valid_start_date	valid_end_date	invalid_reason' \
'201820	Diabetes mellitus	Condition	SNOMED	Clinical Finding	S	73211009	19700101	20991231	' \
'201826	Type 2 diabetes mellitus	Condition	SNOMED	Clinical Finding	S	44054006	19700101	20991231	' \
'4193704	Type 2 diabetes mellitus without complication	Condition	SNOMED	Clinical Finding	S	313436004	19700101	20991231	' \
'1567956	Type 2 diabetes mellitus	Condition	ICD10CM	3-char nonbill code		E11	19700101	20991231	' \
'9000001	Fixture "quoted" name with \backslash	Observation	SNOMED	Clinical Finding	S	FIXTURE1	19700101	20991231	' \
'4180186	English language	Language	SNOMED	Qualifier Value	S	297496009	19700101	20991231	' \
> $F/CONCEPT.csv

printf '%s\n' \
'vocabulary_id	vocabulary_name	vocabulary_reference	vocabulary_version	vocabulary_concept_id' \
'None	OMOP Standardized Vocabularies	OMOP generated	v5.0 FIXTURE-01-JAN-26	44819096' \
'SNOMED	Systematic Nomenclature of Medicine - Clinical Terms (IHTSDO)	http://www.snomed.org/	FIXTURE	44819097' \
'ICD10CM	International Classification of Diseases, Tenth Revision, Clinical Modification (NCHS)	http://www.cdc.gov/nchs/icd/icd10cm.htm	FIXTURE	44819098' \
> $F/VOCABULARY.csv

printf '%s\n' \
'domain_id	domain_name	domain_concept_id' \
'Condition	Condition	19' \
'Observation	Observation	27' \
'Language	Language	0' \
> $F/DOMAIN.csv

printf '%s\n' \
'concept_class_id	concept_class_name	concept_class_concept_id' \
'Clinical Finding	Clinical Finding	0' \
'3-char nonbill code	3-character non-billable code	0' \
'Qualifier Value	Qualifier value	0' \
> $F/CONCEPT_CLASS.csv

printf '%s\n' \
'relationship_id	relationship_name	is_hierarchical	defines_ancestry	reverse_relationship_id	relationship_concept_id' \
'Is a	Is a (SNOMED)	1	1	Subsumes	44818821' \
'Subsumes	Subsumes (SNOMED)	1	1	Is a	44818723' \
'Maps to	Non-standard to Standard map (OMOP)	0	0	Mapped from	44818977' \
'Mapped from	Standard to Non-standard map (OMOP)	0	0	Maps to	44818978' \
> $F/RELATIONSHIP.csv

printf '%s\n' \
'concept_id_1	concept_id_2	relationship_id	valid_start_date	valid_end_date	invalid_reason' \
'201826	201820	Is a	19700101	20991231	' \
'201820	201826	Subsumes	19700101	20991231	' \
'4193704	201826	Is a	19700101	20991231	' \
'201826	4193704	Subsumes	19700101	20991231	' \
'1567956	201826	Maps to	19700101	20991231	' \
'201826	1567956	Mapped from	19700101	20991231	' \
> $F/CONCEPT_RELATIONSHIP.csv

printf '%s\n' \
'ancestor_concept_id	descendant_concept_id	min_levels_of_separation	max_levels_of_separation' \
'201820	201820	0	0' \
'201826	201826	0	0' \
'4193704	4193704	0	0' \
'9000001	9000001	0	0' \
'201820	201826	1	1' \
'201820	4193704	2	2' \
'201826	4193704	1	1' \
> $F/CONCEPT_ANCESTOR.csv

printf '%s\n' \
'concept_id	concept_synonym_name	language_concept_id' \
'201826	T2DM - Type 2 diabetes mellitus	4180186' \
> $F/CONCEPT_SYNONYM.csv

printf '%s\n' \
'drug_concept_id	ingredient_concept_id	amount_value	amount_unit_concept_id	numerator_value	numerator_unit_concept_id	denominator_value	denominator_unit_concept_id	box_size	valid_start_date	valid_end_date	invalid_reason' \
> $F/DRUG_STRENGTH.csv
```

Verify that the tabs survived: `awk -F'\t' 'NR==2{print NF}' atlas/tests/fixtures/vocab-mini/CONCEPT.csv`. Expected: `10`.

`DRUG_STRENGTH.csv` is intentionally header-only, to exercise loading an empty table.

- [ ] **Step 2: Add the `vocab` layer to the smoke tests (the failing test)**

Add this function to `atlas/tests/smoke.sh`, directly after `layer_base`:

```bash
layer_vocab() {
  echo "== vocab"
  if ./load-vocab.sh "$FIXTURE"; then pass "loader succeeds on fixture"; else fail "loader succeeds on fixture"; return; fi

  assert_contains "VOCAB source registered" \
    "$(curl -fsS "$WEBAPI/source/sources")" '"sourceKey":"VOCAB"'
  assert_contains "search finds T2DM (201826)" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'
  # JSON-escaped form of: Fixture "quoted" name with \backslash
  assert_contains "quote and backslash survive loading" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/concept/9000001")" 'Fixture \"quoted\" name with \\backslash'
  assert_contains "descendants resolve via concept_ancestor" \
    "$(curl -fsS -X POST -H 'Content-Type: application/json' \
        -d '{"items":[{"concept":{"CONCEPT_ID":201820},"isExcluded":false,"includeDescendants":true,"includeMapped":false}]}' \
        "$WEBAPI/vocabulary/VOCAB/resolveConceptSetExpression")" '4193704'

  local cs_id
  cs_id=$(curl -fsS -X POST -H 'Content-Type: application/json' \
            -d "{\"name\":\"smoke-$(date +%s)\"}" "$WEBAPI/conceptset/" | json 'd["id"]')
  if [[ "$cs_id" =~ ^[0-9]+$ ]]; then pass "concept set created (id $cs_id)"; else fail "concept set created"; return; fi
  assert_eq "concept set items saved" \
    "$(curl -fsS -X PUT -H 'Content-Type: application/json' \
        -d '[{"conceptId":201820,"isExcluded":0,"includeDescendants":1,"includeMapped":0}]' \
        "$WEBAPI/conceptset/$cs_id/items")" "true"
  assert_eq "concept set expression reads back" \
    "$(curl -fsS "$WEBAPI/conceptset/$cs_id/expression" \
        | json '(d["items"][0]["concept"]["CONCEPT_ID"], d["items"][0]["includeDescendants"])')" \
    "(201820, True)"

  if ./load-vocab.sh "$FIXTURE" >/dev/null; then pass "loader rerun succeeds"; else fail "loader rerun succeeds"; fi
  assert_eq "rerun leaves exactly one VOCAB source" \
    "$(psql_q "select count(*) from webapi.source where source_key = 'VOCAB'")" "1"
  assert_eq "rerun leaves one vocabulary daimon" \
    "$(psql_q "select count(*) from webapi.source_daimon d join webapi.source s using (source_id) where s.source_key = 'VOCAB'")" "1"
  if curl -fsS "$WEBAPI/conceptset/$cs_id" >/dev/null; then pass "concept set survives reload"; else fail "concept set survives reload"; fi

  local tmp out
  tmp=$(mktemp -d)
  cp "$FIXTURE"/*.csv "$tmp"/
  rm "$tmp/CONCEPT_ANCESTOR.csv" "$tmp/DOMAIN.csv"
  if out=$(./load-vocab.sh "$tmp" 2>&1); then fail "incomplete bundle is rejected"; else pass "incomplete bundle is rejected"; fi
  assert_contains "error names CONCEPT_ANCESTOR.csv" "$out" "CONCEPT_ANCESTOR.csv"
  assert_contains "error names DOMAIN.csv" "$out" "DOMAIN.csv"
  assert_eq "existing vocab untouched" "$(psql_q "select count(*) from vocab.concept")" "6"
  rm -rf "$tmp"
}
```

Replace the `case` block with:

```bash
case "${1:-}" in
  base)  layer_base ;;
  vocab) layer_base && layer_vocab ;;
  all)   layer_base && layer_vocab ;;
  down)  dc --profile load down -v; exit 0 ;;
  *)     echo "usage: $0 base|vocab|solr|all|down" >&2; exit 2 ;;
esac
```

- [ ] **Step 3: Run it to verify it fails**

Run: `atlas/tests/smoke.sh vocab`
Expected: the base checks pass, then `FAIL loader succeeds on fixture` (`./load-vocab.sh: No such file or directory`), exit 1.

- [ ] **Step 4: Write the SQL**

`atlas/sql/vocab_ddl.sql`, OMOP CDM v5.4 vocabulary tables with no constraints (they're added after loading, which is faster):

```sql
-- OMOP CDM v5.4 vocabulary tables. Recreated from scratch on every load.
DROP SCHEMA IF EXISTS vocab CASCADE;
CREATE SCHEMA vocab;

CREATE TABLE vocab.concept (
  concept_id integer NOT NULL,
  concept_name varchar(255) NOT NULL,
  domain_id varchar(20) NOT NULL,
  vocabulary_id varchar(20) NOT NULL,
  concept_class_id varchar(20) NOT NULL,
  standard_concept varchar(1) NULL,
  concept_code varchar(50) NOT NULL,
  valid_start_date date NOT NULL,
  valid_end_date date NOT NULL,
  invalid_reason varchar(1) NULL );

CREATE TABLE vocab.vocabulary (
  vocabulary_id varchar(20) NOT NULL,
  vocabulary_name varchar(255) NOT NULL,
  vocabulary_reference varchar(255) NULL,
  vocabulary_version varchar(255) NULL,
  vocabulary_concept_id integer NOT NULL );

CREATE TABLE vocab.domain (
  domain_id varchar(20) NOT NULL,
  domain_name varchar(255) NOT NULL,
  domain_concept_id integer NOT NULL );

CREATE TABLE vocab.concept_class (
  concept_class_id varchar(20) NOT NULL,
  concept_class_name varchar(255) NOT NULL,
  concept_class_concept_id integer NOT NULL );

CREATE TABLE vocab.concept_relationship (
  concept_id_1 integer NOT NULL,
  concept_id_2 integer NOT NULL,
  relationship_id varchar(20) NOT NULL,
  valid_start_date date NOT NULL,
  valid_end_date date NOT NULL,
  invalid_reason varchar(1) NULL );

CREATE TABLE vocab.relationship (
  relationship_id varchar(20) NOT NULL,
  relationship_name varchar(255) NOT NULL,
  is_hierarchical varchar(1) NOT NULL,
  defines_ancestry varchar(1) NOT NULL,
  reverse_relationship_id varchar(20) NOT NULL,
  relationship_concept_id integer NOT NULL );

CREATE TABLE vocab.concept_synonym (
  concept_id integer NOT NULL,
  concept_synonym_name varchar(1000) NOT NULL,
  language_concept_id integer NOT NULL );

CREATE TABLE vocab.concept_ancestor (
  ancestor_concept_id integer NOT NULL,
  descendant_concept_id integer NOT NULL,
  min_levels_of_separation integer NOT NULL,
  max_levels_of_separation integer NOT NULL );

CREATE TABLE vocab.drug_strength (
  drug_concept_id integer NOT NULL,
  ingredient_concept_id integer NOT NULL,
  amount_value numeric NULL,
  amount_unit_concept_id integer NULL,
  numerator_value numeric NULL,
  numerator_unit_concept_id integer NULL,
  denominator_value numeric NULL,
  denominator_unit_concept_id integer NULL,
  box_size integer NULL,
  valid_start_date date NOT NULL,
  valid_end_date date NOT NULL,
  invalid_reason varchar(1) NULL );
```

`atlas/sql/vocab_indexes.sql`:

```sql
-- Keys and indexes from the OMOP CDM v5.4 Postgres scripts, minus CLUSTER
-- (it rewrites each table, doubling disk use) and minus single-column
-- indexes that duplicate a primary key.
ALTER TABLE vocab.concept ADD CONSTRAINT xpk_concept PRIMARY KEY (concept_id);
ALTER TABLE vocab.vocabulary ADD CONSTRAINT xpk_vocabulary PRIMARY KEY (vocabulary_id);
ALTER TABLE vocab.domain ADD CONSTRAINT xpk_domain PRIMARY KEY (domain_id);
ALTER TABLE vocab.concept_class ADD CONSTRAINT xpk_concept_class PRIMARY KEY (concept_class_id);
ALTER TABLE vocab.relationship ADD CONSTRAINT xpk_relationship PRIMARY KEY (relationship_id);

CREATE INDEX idx_concept_code ON vocab.concept (concept_code);
CREATE INDEX idx_concept_vocabluary_id ON vocab.concept (vocabulary_id);
CREATE INDEX idx_concept_domain_id ON vocab.concept (domain_id);
CREATE INDEX idx_concept_class_id ON vocab.concept (concept_class_id);
CREATE INDEX idx_concept_relationship_id_1 ON vocab.concept_relationship (concept_id_1);
CREATE INDEX idx_concept_relationship_id_2 ON vocab.concept_relationship (concept_id_2);
CREATE INDEX idx_concept_relationship_id_3 ON vocab.concept_relationship (relationship_id);
CREATE INDEX idx_concept_synonym_id ON vocab.concept_synonym (concept_id);
CREATE INDEX idx_concept_ancestor_id_1 ON vocab.concept_ancestor (ancestor_concept_id);
CREATE INDEX idx_concept_ancestor_id_2 ON vocab.concept_ancestor (descendant_concept_id);
CREATE INDEX idx_drug_strength_id_1 ON vocab.drug_strength (drug_concept_id);
CREATE INDEX idx_drug_strength_id_2 ON vocab.drug_strength (ingredient_concept_id);

ANALYZE;
```

`atlas/sql/register_source.sql` (psql variables `jdbc_url`, `db_user`, `db_password`):

```sql
-- Register the vocab schema with WebAPI as a vocabulary-only source.
-- Idempotent: relies on source_key_unique and un_source_daimon constraints.
INSERT INTO webapi.source
  (source_name, source_key, source_connection, source_dialect, username, password, is_cache_enabled)
VALUES
  ('OMOP Vocabulary', 'VOCAB', :'jdbc_url', 'postgresql', :'db_user', :'db_password', false)
ON CONFLICT (source_key) DO UPDATE SET
  source_connection = EXCLUDED.source_connection,
  username = EXCLUDED.username,
  password = EXCLUDED.password,
  deleted_date = NULL;

-- daimon_type 1 = Vocabulary (SourceDaimon.DaimonType ordinal)
INSERT INTO webapi.source_daimon (source_id, daimon_type, table_qualifier, priority)
SELECT source_id, 1, 'vocab', 1 FROM webapi.source WHERE source_key = 'VOCAB'
ON CONFLICT (source_id, daimon_type) DO UPDATE SET
  table_qualifier = EXCLUDED.table_qualifier,
  priority = EXCLUDED.priority;
```

- [ ] **Step 5: Write the in-container loader**

`atlas/scripts/load-vocab-db.sh`:

```bash
#!/usr/bin/env bash
# Runs inside the vocab-loader container (postgres image, for psql).
# Recreates the vocab schema from the Athena CSVs mounted at /vocab and
# registers it with WebAPI. Bundle validation happens on the host first.
set -euo pipefail

TABLES=(concept vocabulary domain concept_class concept_relationship
        relationship concept_synonym concept_ancestor drug_strength)

psql_run() { psql -v ON_ERROR_STOP=1 -q "$@"; }

echo "  creating vocab schema"
psql_run -f /sql/vocab_ddl.sql

for t in "${TABLES[@]}"; do
  start=$SECONDS
  # Athena files are tab-separated with literal quote characters in text, so
  # disable CSV quoting by setting the quote char to backspace.
  psql_run -c "\\copy vocab.$t FROM '/vocab/${t^^}.csv' WITH (FORMAT csv, DELIMITER E'\\t', QUOTE E'\\b', HEADER true)"
  echo "  loaded $t: $(psql -tAc "select count(*) from vocab.$t") rows ($((SECONDS - start))s)"
done

echo "  adding keys and indexes"
psql_run -f /sql/vocab_indexes.sql

echo "  registering VOCAB source with WebAPI"
psql_run -v jdbc_url="jdbc:postgresql://db:5432/$PGDATABASE" \
         -v db_user="$PGUSER" -v db_password="$PGPASSWORD" \
         -f /sql/register_source.sql
```

- [ ] **Step 6: Add the `vocab-loader` service to compose**

In `atlas/docker-compose.yml`, add this after the `atlas` service and before `volumes:`:

```yaml
  # One-shot loader; never starts with a plain `up`. Run via ./load-vocab.sh.
  vocab-loader:
    image: postgres:16
    profiles: [load]
    depends_on:
      db:
        condition: service_healthy
    environment:
      PGHOST: db
      PGDATABASE: ${POSTGRES_DB:-ohdsi}
      PGUSER: ${POSTGRES_USER:-ohdsi}
      PGPASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - type: bind
        source: ${VOCAB_DIR:-./.vocab-dir-not-set}
        target: /vocab
        read_only: true
        bind:
          create_host_path: false
      - ./sql:/sql:ro
      - ./scripts:/scripts:ro
    entrypoint: ["bash", "/scripts/load-vocab-db.sh"]
```

- [ ] **Step 7: Write the host-side loader**

`atlas/load-vocab.sh`:

```bash
#!/usr/bin/env bash
# Load an Athena vocabulary bundle into the stack and register it with WebAPI.
#
# Usage (from anywhere):  atlas/load-vocab.sh [/path/to/athena/bundle]
# The path defaults to VOCAB_DIR from the env file. ENV_FILE selects the env
# file (default .env, relative to this directory).
set -euo pipefail
cd "$(dirname "$0")"

die() { echo "ERROR: $*" >&2; exit 1; }

ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found (copy .env.example to .env)"
set -a; . "./$ENV_FILE"; set +a
[[ $# -ge 1 ]] && VOCAB_DIR="$1"
export VOCAB_DIR

WEBAPI="http://localhost:${WEBAPI_PORT:-8080}/WebAPI"
REQUIRED=(CONCEPT CONCEPT_ANCESTOR CONCEPT_CLASS CONCEPT_RELATIONSHIP
          CONCEPT_SYNONYM DOMAIN DRUG_STRENGTH RELATIONSHIP VOCABULARY)

dc() { docker compose --env-file "$ENV_FILE" "$@"; }

step() { # label cmd...
  local label=$1 start=$SECONDS; shift
  echo "==> $label"
  "$@"
  echo "    done in $((SECONDS - start))s"
}

wait_webapi() {
  for _ in $(seq 1 60); do
    curl -fs "$WEBAPI/info" >/dev/null && return 0
    sleep 5
  done
  die "WebAPI did not become ready at $WEBAPI"
}

solr_running() { dc ps --status running --services | grep -qx solr; }

# Validate before touching anything, so a bad bundle leaves the current vocab intact.
[[ -n "${VOCAB_DIR:-}" ]] || die "no vocabulary directory: pass one or set VOCAB_DIR in $ENV_FILE"
[[ -d "$VOCAB_DIR" ]] || die "vocabulary directory not found: $VOCAB_DIR"
missing=()
for t in "${REQUIRED[@]}"; do
  [[ -f "$VOCAB_DIR/$t.csv" ]] || missing+=("$t.csv")
done
(( ${#missing[@]} == 0 )) || die "$VOCAB_DIR is missing: ${missing[*]}"

step "Starting stack" dc up -d --wait
step "Loading vocabulary from $VOCAB_DIR" dc --profile load run --rm vocab-loader
step "Refreshing WebAPI source cache" curl -fsS -o /dev/null "$WEBAPI/source/refresh"
echo "Vocabulary loaded. ATLAS: http://localhost:${ATLAS_PORT:-8081}/atlas/"
```

Run: `chmod +x atlas/load-vocab.sh atlas/scripts/load-vocab-db.sh`

- [ ] **Step 8: Run the tests to verify they pass**

Run: `atlas/tests/smoke.sh vocab`
Expected: all `base` and `vocab` checks `ok`, then `all checks passed`.

If `search finds T2DM` or `VOCAB source registered` fails, check WebAPI's logs first (`docker compose --env-file tests/test.env logs webapi | tail -100`), looking for an error about a missing CDM or Results daimon. This is the spec's open question. The fallback: add `CREATE SCHEMA IF NOT EXISTS results;` at the top of `register_source.sql`, plus a second daimon insert `SELECT source_id, 2, 'results', 1 ...` with the same `ON CONFLICT` clause, then rerun. Record in the commit message which way it went.

- [ ] **Step 9: Commit**

```bash
git add atlas/tests/fixtures atlas/sql atlas/scripts atlas/load-vocab.sh atlas/docker-compose.yml atlas/tests/smoke.sh
git commit -m "atlas: vocab loader, WebAPI source registration, fixture-based smoke tests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Solr vocabulary search with `solr` smoke layer

**Files:**
- Create: `atlas/solr/Dockerfile`
- Create: `atlas/solr/conf/` (vendored), `atlas/solr/conf/data-config.xml`
- Modify: `atlas/solr/conf/solrconfig.xml` (the `/dataimport` request handler block)
- Modify: `atlas/docker-compose.yml` (add `solr`, a `solrdata` volume, and webapi `depends_on` solr)
- Modify: `atlas/load-vocab.sh` (add `build_solr_index`, restart WebAPI after indexing)
- Modify: `atlas/tests/smoke.sh` (add `layer_solr`, wire into `case`)

**Interfaces:**
- Consumes: from Task 2, `load-vocab.sh` helpers `die`, `dc`, `step`, `wait_webapi`, `solr_running`, plus the fixture version `v5.0 FIXTURE-01-JAN-26`.
- Produces: compose service `solr` (port 8983 in the container, `${SOLR_PORT}` on the host at 127.0.0.1); configset `omop_vocab`; one Solr core per vocab version, named `vocabulary_version` with spaces replaced by `_`.

- [ ] **Step 1: Add the `solr` layer to the smoke tests (the failing test)**

Add this function to `atlas/tests/smoke.sh`, after `layer_vocab`:

```bash
layer_solr() {
  echo "== solr"
  local core="v5.0_FIXTURE-01-JAN-26" solr="http://localhost:${SOLR_PORT}/solr"
  if ./load-vocab.sh "$FIXTURE"; then pass "loader succeeds with solr"; else fail "loader succeeds with solr"; return; fi

  assert_contains "core named from vocab version exists" \
    "$(curl -fsS "$solr/admin/cores?action=STATUS&core=$core&wt=json")" "\"name\":\"$core\""
  assert_eq "core is populated" \
    "$(curl -fsS "$solr/$core/select?q=query:diabetes&rows=0&wt=json" | json 'd["response"]["numFound"] >= 4')" "True"
  assert_contains "WebAPI search returns results" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'

  # WebAPI has no SQL fallback once a Solr core matches: if Solr is down,
  # search is empty. That proves WebAPI is really using Solr.
  dc stop solr >/dev/null 2>&1
  assert_eq "WebAPI search is empty with Solr stopped (uses Solr)" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" "[]"
  dc up -d --wait solr >/dev/null 2>&1
  assert_contains "WebAPI search recovers when Solr returns" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'
}
```

Replace the `case` block with:

```bash
case "${1:-}" in
  base)  layer_base ;;
  vocab) layer_base && layer_vocab ;;
  solr)  layer_base && layer_solr ;;
  all)   layer_base && layer_vocab && layer_solr ;;
  down)  dc --profile load down -v; exit 0 ;;
  *)     echo "usage: $0 base|vocab|solr|all|down" >&2; exit 2 ;;
esac
```

- [ ] **Step 2: Run it to verify it fails**

Run: `atlas/tests/smoke.sh solr`
Expected: `FAIL core named from vocab version exists` (connection refused on port 18983, because there's no Solr service yet), exit 1.

- [ ] **Step 3: Vendor the Solr configset from Broadsea**

```bash
SHA=7ca896ae7d2c33f5197a7b47aec0a29d1e897d2f
tmp=$(mktemp -d)
curl -fsSL "https://codeload.github.com/OHDSI/Broadsea/tar.gz/$SHA" | tar -xz -C "$tmp"
mkdir -p atlas/solr
cp -R "$tmp/Broadsea-$SHA/solr/conf" atlas/solr/conf
rm -f atlas/solr/conf/dataimport.properties   # runtime state, not config
rm -rf "$tmp"
ls atlas/solr/conf
```

Expected: `lang  managed-schema  params.json  protwords.txt  solrconfig.xml  stopwords.txt  synonyms.txt`

`atlas/solr/conf/data-config.xml` (Broadsea keeps this outside `conf/`; a configset needs it inside):

```xml
<!-- DataImportHandler config: pulls OMOP concepts from Postgres.
     Adapted from OHDSI Broadsea (Apache-2.0), commit 7ca896a. -->
<dataConfig>
 <document name='concepts'>
   <entity name="concept"
           dataSource="omop_vocab"
           pk="concept_id"
           query="select c.concept_id,
       c.concept_code,
       c.concept_name,
       c.concept_class_id,
       c.domain_id,
       c.vocabulary_id,
       c.standard_concept,
       c.invalid_reason,
       d.domain_name,
       v.vocabulary_name,
       c.valid_start_date,
       c.valid_end_date
       from ${dataimporter.request.vocab_database_schema}.concept as c
       join ${dataimporter.request.vocab_database_schema}.domain as d on c.domain_id = d.domain_id
       join ${dataimporter.request.vocab_database_schema}.vocabulary as v on c.vocabulary_id = v.vocabulary_id"
            preImportDeleteQuery="*:*">
     <field column="concept_id" name="id" />
     <field column="concept_code" name="concept_code" />
     <field column="concept_name" name="concept_name" />
     <field column="concept_class_id" name="concept_class_id" />
     <field column="domain_id" name="domain_id" />
     <field column="vocabulary_id" name="vocabulary_id" />
     <field column="standard_concept" name="standard_concept" />
     <field column="invalid_reason" name="invalid_reason" />
     <field column="domain_name" name="domain_name" />
     <field column="vocabulary_name" name="vocabulary_name" />
     <field column="valid_start_date" name="valid_start_date" />
     <field column="valid_end_date" name="valid_end_date" />
   </entity>
 </document>
</dataConfig>
```

In `atlas/solr/conf/solrconfig.xml`, replace the whole `<requestHandler name="/dataimport" class="solr.DataImportHandler">...</requestHandler>` block (about lines 759–770) with:

```xml
	<!-- Datasource values come from -D system properties set in
	     docker-compose.yml (SOLR_OPTS). batchSize becomes the JDBC fetch
	     size; with DIH's default autoCommit=false, Postgres streams rows via
	     a cursor instead of buffering the whole concept table in the heap. -->
	<requestHandler name="/dataimport" class="solr.DataImportHandler">
	<lst name="defaults">
		<str name="config">data-config.xml</str>
		<lst name="datasource">
			<str name="name">omop_vocab</str>
			<str name="driver">org.postgresql.Driver</str>
			<str name="url">${VOCAB_JDBC_URL}</str>
			<str name="user">${VOCAB_JDBC_USER}</str>
			<str name="password">${VOCAB_JDBC_PASSWORD}</str>
			<str name="batchSize">10000</str>
		</lst>
	</lst>
	</requestHandler>
```

Verify: `grep -c 'VOCAB_JDBC_DRIVER_PATH' atlas/solr/conf/solrconfig.xml`. Expected: `0`.

- [ ] **Step 4: Write the Solr Dockerfile and compose service**

`atlas/solr/Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
# Solr 8 (not 9: the DataImportHandler used to pull concepts from Postgres
# was removed in 9) with the Postgres JDBC driver and the omop_vocab configset.
FROM solr:8.11.4
USER root
# solrconfig.xml loads every jar in contrib/dataimporthandler-extras/lib.
ADD --chmod=644 https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar \
    /opt/solr/contrib/dataimporthandler-extras/lib/postgresql-42.7.4.jar
COPY conf /opt/solr/server/solr/configsets/omop_vocab/conf
USER solr
```

In `atlas/docker-compose.yml`, add this service after `db`:

```yaml
  solr:
    build: ./solr
    image: atlas-workshop/solr:8.11.4-omop
    restart: unless-stopped
    environment:
      SOLR_HEAP: ${SOLR_HEAP:-1g}
      SOLR_OPTS: >-
        -DVOCAB_JDBC_URL=jdbc:postgresql://db:5432/${POSTGRES_DB:-ohdsi}
        -DVOCAB_JDBC_USER=${POSTGRES_USER:-ohdsi}
        -DVOCAB_JDBC_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - solrdata:/var/solr
    ports:
      - "127.0.0.1:${SOLR_PORT:-8983}:8983"
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:8983/solr/admin/info/system >/dev/null"]
      interval: 10s
      timeout: 5s
      retries: 20
```

Add `solr` to the `webapi` service's `depends_on` (WebAPI scans Solr cores once at startup, so Solr must be reachable by then):

```yaml
    depends_on:
      db:
        condition: service_healthy
      solr:
        condition: service_healthy
```

Change the top-level `volumes:` block to:

```yaml
volumes:
  pgdata:
  solrdata:
```

- [ ] **Step 5: Add Solr indexing and the WebAPI restart to the loader**

In `atlas/load-vocab.sh`, add this function after `solr_running()`:

```bash
build_solr_index() {
  local solr="http://localhost:${SOLR_PORT:-8983}/solr" version core status
  version=$(dc exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "select vocabulary_version from vocab.vocabulary where vocabulary_id = 'None'")
  [[ -n "$version" ]] || die "no 'None' row in vocab.vocabulary; cannot name the Solr core"
  # WebAPI looks cores up by this exact transformation of the version string.
  core="${version// /_}"
  echo "    core: $core"

  if ! curl -fsS "$solr/admin/cores?action=STATUS&core=$core&wt=json" | grep -q "\"name\":\"$core\""; then
    curl -fsS -o /dev/null "$solr/admin/cores?action=CREATE&name=$core&configSet=omop_vocab"
  fi
  curl -fsS -o /dev/null \
    "$solr/$core/dataimport?command=full-import&entity=concept&clean=true&commit=true&vocab_database_schema=vocab"
  sleep 2
  while status=$(curl -fsS "$solr/$core/dataimport?command=status&wt=json"); \
        grep -q '"status":"busy"' <<<"$status"; do
    sleep 5
  done
  grep -q 'Indexing completed' <<<"$status" || die "Solr import failed: $status"
}
```

Replace the last two lines of the script (the `Refreshing WebAPI source cache` step and the final `echo`) with:

```bash
if solr_running; then
  step "Building Solr index" build_solr_index
  # WebAPI reads the Solr core list only at startup.
  step "Restarting WebAPI" dc restart webapi
  step "Waiting for WebAPI" wait_webapi
else
  echo "==> Solr not running; skipping index (search uses SQL)"
  step "Refreshing WebAPI source cache" curl -fsS -o /dev/null "$WEBAPI/source/refresh"
fi
echo "Vocabulary loaded. ATLAS: http://localhost:${ATLAS_PORT:-8081}/atlas/"
```

`wait_webapi` can return immediately if it polls before the restarted container has stopped. `docker compose restart` blocks until the container is restarted, so the old process is already gone by the time `wait_webapi` runs.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `atlas/tests/smoke.sh all`
Expected: every `base`, `vocab` and `solr` check is `ok`, then `all checks passed`. (The `vocab` layer's search checks now also go through Solr.)

If `core is populated` fails, read `docker compose --env-file tests/test.env logs solr | grep -iE 'error|exception' | head`. A `ClassNotFoundException: org.postgresql.Driver` means the jar isn't on the DIH classpath. Confirm with `docker compose --env-file tests/test.env exec solr ls /opt/solr/contrib/dataimporthandler-extras/lib/`.

If `WebAPI search is empty with Solr stopped` fails because results come back, WebAPI is not using Solr. Check `docker compose --env-file tests/test.env logs webapi | grep -i solr` for `SOLR Core Initialization Error`.

- [ ] **Step 7: Commit**

```bash
git add atlas/solr atlas/docker-compose.yml atlas/load-vocab.sh atlas/tests/smoke.sh
git commit -m "atlas: Solr vocabulary search (configset from Broadsea, DIH import, WebAPI restart)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: README and a real-vocabulary run

**Files:**
- Create: `atlas/README.md`
- Modify: `atlas/README.md` again in Step 4 with measured timings

**Interfaces:**
- Consumes: everything above. A real Athena dev bundle (SNOMED, ICD10CM, RxNorm) from the user.
- Produces: the user-facing docs. Measured load and index timings for server sizing.

- [ ] **Step 1: Write the README**

`atlas/README.md`:

````markdown
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
3. `./load-vocab.sh`. The first run builds WebAPI from source (10–20 min),
   then loads the vocabulary, builds the Solr index and restarts WebAPI.
4. Open http://localhost:8081/atlas/.

To load a different bundle later: `./load-vocab.sh /path/to/other/bundle`.
The vocab schema is replaced, and saved concept sets are kept.

Day to day: `docker compose up -d` / `docker compose down`. Data lives in the
`pgdata` and `solrdata` volumes. `docker compose down -v` deletes them
(including saved concept sets).

## Sizing

| Bundle | Disk (Postgres) | Load | Solr index |
|---|---|---|---|
| Dev subset (SNOMED, ICD10CM, RxNorm) | MEASURED_DEV_DISK | MEASURED_DEV_LOAD | MEASURED_DEV_INDEX |
| Full Athena download | ~15–25 GB (estimate) | — | — |

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
````

- [ ] **Step 2: Tear down the test project to free memory and disk**

Run: `atlas/tests/smoke.sh down`
Expected: the `atlas-test` containers and volumes are removed.

- [ ] **Step 3: Real-vocabulary run (needs the user)**

Ask the user for the path to their Athena dev bundle. Then:

```bash
cd atlas && cp .env.example .env
# edit .env: POSTGRES_PASSWORD=<something>, VOCAB_DIR=<user's path>
./load-vocab.sh 2>&1 | tee /tmp/atlas-load.log
docker compose exec -T db psql -U ohdsi -d ohdsi -tAc "select pg_size_pretty(pg_database_size('ohdsi'))"
docker stats --no-stream solr
```

Expected: the loader prints `done in Ns` for each step and ends with `Vocabulary loaded.` Solr memory stays well below `SOLR_HEAP` during indexing (this is Review Focus 5: an OOM here means DIH is buffering rather than streaming).

Then the manual check in the browser at http://localhost:8081/atlas/, which the user does or watches:
1. Search → "type 2 diabetes". SNOMED 201826 appears, and the results come back in about a second.
2. Open 201826. The Hierarchy tab shows parents and children, and Related Concepts lists the ICD10CM E11 mapping.
3. Concept Sets → New. Add 201826 with Descendants, save it as "T2DM test", reload the page, and confirm it's still there with the same items.
4. Export the concept set (Export tab → CSV/JSON) and confirm the download works.

- [ ] **Step 4: Record the measured timings in the README**

In `atlas/README.md`, replace `MEASURED_DEV_DISK`, `MEASURED_DEV_LOAD` and `MEASURED_DEV_INDEX` with the values from Step 3: the database size, the `Loading vocabulary` step time, and the `Building Solr index` step time.

- [ ] **Step 5: Commit**

```bash
git add atlas/README.md
git commit -m "atlas: README with quick start, sizing from dev-subset run, troubleshooting

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
