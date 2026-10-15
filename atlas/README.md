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

## Requirements

Docker with Compose v2 (`docker compose`, needs `--wait` support) and `curl`.
`python3` is needed only for `tests/smoke.sh`.

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
(including saved concept sets); if `PGDATA_HOST_PATH` is set, Postgres data
is in that host folder instead, which `down -v` does not delete.

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

## Deploying to DigitalOcean

The plan: build and load everything on a droplet once, snapshot it, destroy
the droplet, and recreate it from the snapshot for the workshop. DigitalOcean
bills powered-off droplets at the full rate, but a snapshot only costs about
$0.06/GB-month of used disk (roughly $1-2/month here). The droplet itself is
billed hourly only while it exists.

On the server, Caddy (compose profile `proxy`) is the only public entry point
on port 80 (and 443 with HTTPS). It blocks WebAPI source administration
and can put one shared password in front of ATLAS and WebAPI. WebAPI and ATLAS
listen on localhost only.

### 1. Cloud Firewall (once)

Use a DigitalOcean Cloud Firewall, not `ufw`: Docker's published ports bypass
`ufw`, while the Cloud Firewall filters traffic before it reaches the droplet.

1. **Networking → Firewalls → Create Firewall**, name it `atlas-workshop`.
2. **Inbound rules:**
   - SSH (TCP 22): replace the default "All IPv4 / All IPv6" sources with your
     own IP address (a search for "what is my IP" shows it). If your IP
     changes, edit this rule; the droplet's web **Console** works regardless.
   - Add **HTTP** (TCP 80) and **HTTPS** (TCP 443) from All IPv4 and All IPv6.
3. **Outbound rules:** leave the defaults (all allowed; needed for apt,
   Docker Hub, GitHub and Let's Encrypt).
4. **Apply to Droplets:** enter the tag `atlas-workshop`. Any droplet with
   that tag gets the firewall automatically, including ones recreated from
   the snapshot.

### 2. Create and set up the droplet

1. **Create → Droplets:** Ubuntu 24.04 LTS, Basic, 4 vCPU / 8 GB / 160 GB
   (see [Sizing](#sizing)), your SSH key, and the tag `atlas-workshop`.
2. On the droplet (SSH, or the droplet's web **Console**), clone the repo
   into `/root` and run the setup script (Docker, swap, log rotation,
   key-only SSH, automatic security updates):

   ```bash
   cd /root
   git clone -b atlas https://github.com/rwdcollaborative/amia-2026-rwd-workshop.git
   bash /root/amia-2026-rwd-workshop/atlas/deploy/setup-droplet.sh
   ```

   Everything below runs in `/root/amia-2026-rwd-workshop/atlas`.
3. Upload the Athena zip **from your laptop**. Athena names it
   `vocabulary_download_v5_{<uuid>}_<number>.zip`. Keep the `~/` outside the
   quotes so it expands, and don't backslash-escape the braces inside them:

   ```bash
   rsync -avP ~/"Downloads/vocabulary_download_v5_{<uuid>}_<number>.zip" root@<ip>:/root/vocab.zip
   ```

   `-P` shows progress and resumes an interrupted upload if you rerun it.
   Uploading needs SSH to the droplet: the firewall must allow your IP, and
   the droplet must have your laptop's key. Then unzip on the droplet:

   ```bash
   mkdir -p /root/vocab && unzip /root/vocab.zip -d /root/vocab && ls /root/vocab
   ```

   The CSVs (`CONCEPT.csv`, ...) should be directly in `/root/vocab`. If they
   are in a subfolder, use that for `VOCAB_DIR`.
4. Create `.env` from `.env.example`
   (`cd /root/amia-2026-rwd-workshop/atlas && cp .env.example .env`), with
   these settings:

   ```bash
   VOCAB_DIR=/root/vocab
   POSTGRES_PASSWORD=<openssl rand -hex 16>
   BIND_ADDR=127.0.0.1
   WEBAPI_PUBLIC_URL=/WebAPI/
   COMPOSE_PROFILES=proxy
   WORKSHOP_PASSWORD=<shared password you give attendees>   # optional
   ```

   Without `WORKSHOP_PASSWORD` there is no login prompt and anyone who finds
   the IP can use ATLAS (see [below](#what-the-proxy-does-and-doesnt-protect)).

5. `./load-vocab.sh` (WebAPI builds from source first, 10-20 min). Then
   open `http://<ip>/` (log in as `workshop` if you set a password).
6. Shrink the snapshot:
   `rm -rf /root/vocab.zip /root/vocab && docker builder prune -af`.

### 3. Snapshot and destroy

1. `poweroff` on the droplet (or **Power → Turn off**).
2. **Snapshots → Take Snapshot**. Wait for it to finish.
3. **Destroy → Destroy this Droplet**. The snapshot stays under
   **Backups & Snapshots → Snapshots**.

### 4. Workshop day

1. **Backups & Snapshots → Snapshots → (your snapshot) → More → Create
   Droplet.** Pick the same size or larger, and add the tag `atlas-workshop`
   (tags are not stored in the snapshot, and the firewall depends on it).
2. The stack starts on boot. Then:

   ```bash
   ssh root@<new-ip>
   apt-get update && apt-get upgrade -y   # the snapshot may be months old
   cd /root/amia-2026-rwd-workshop/atlas && docker compose restart webapi   # see "Search is slow" below
   ```

3. Share `http://<new-ip>/` (e.g. through a short link) and the password,
   if any. No
   config changes are needed for a new IP: ATLAS reaches WebAPI by a
   relative URL.
4. Afterwards, destroy the droplet. Snapshot again first if attendees' saved
   concept sets should be kept.

### HTTPS without a domain (optional)

Plain HTTP sends the shared password unencrypted, which is a modest risk for
a throwaway workshop password. For HTTPS, use the free
[sslip.io](https://sslip.io) wildcard DNS, which maps `203-0-113-7.sslip.io`
to `203.0.113.7`. On the day, set `SITE_ADDRESS=<ip-with-dashes>.sslip.io` in
`.env` and run `docker compose up -d caddy`. Caddy gets a Let's Encrypt
certificate within a minute (`docker compose logs caddy`), and attendees use
`https://<ip-with-dashes>.sslip.io/`. If the certificate fails, set
`SITE_ADDRESS=:80` again and use plain HTTP.

### What the proxy does and doesn't protect

WebAPI runs with security disabled, so every endpoint is anonymous to anyone
who reaches it. Caddy adds the following protections:

- **Shared password (if `WORKSHOP_PASSWORD` is set).** It keeps out scanners
  and drive-by visitors. Without it, anyone who reaches the IP can do what
  attendees can.
  - After the first login, Caddy sets an `HttpOnly` cookie derived from the
    password.
  - ATLAS sends `Authorization: null` on some requests, which replaces the
    browser's saved basic-auth login. The cookie keeps those requests
    working.
- **Source administration blocked.** Requests that would delete or repoint
  the vocabulary source, or add a source with an arbitrary JDBC URL (which
  makes the server connect outbound), get 403. That covers
  `POST/PUT/DELETE /WebAPI/source*` and `/WebAPI/source/details*`.

Anyone with the password can still do anything else ATLAS can do, such as
editing or deleting other attendees' concept sets. If the source is somehow
deleted, run `./load-vocab.sh` again to restore it (this is a full reload). A
read-only database role for the source is a possible further hardening; it
is not implemented.

Without the proxy (e.g. on a private network), set
`WEBAPI_PUBLIC_URL=http://<host>:8080/WebAPI/` and leave `BIND_ADDR` at its
default. Ports 8080 and 8081 are then exposed with no protection at all.

## Troubleshooting

- **Search returns nothing, but the concept set pages work.** Solr is probably
  down. WebAPI does not fall back to SQL once a Solr core exists. Run
  `docker compose ps solr`, then `docker compose up -d solr`.
- **Search is slow / Solr index missing.** Most likely WebAPI started before
  Solr was ready (restart policies ignore `depends_on`, e.g. after a host
  reboot), so it found no cores and uses SQL search. Check the core exists:
  `curl -s 'http://127.0.0.1:8983/solr/admin/cores?action=STATUS&wt=json'`.
  If it does, `docker compose restart webapi` (WebAPI reads the core list
  only at startup). Only if the core is missing, rerun `./load-vocab.sh`.
  After a host reboot, if search is slow, restart webapi.
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

`tests/smoke.sh all` runs layered checks (base stack, vocab loading, Solr,
proxy) against a separate compose project (`tests/test.env`, ports
18080/18081, proxy on 18088) loaded with a tiny synthetic vocabulary in
`tests/fixtures/vocab-mini`. It never touches your dev data.
`tests/smoke.sh down` removes it.

## Credits

Solr configset adapted from [OHDSI Broadsea](https://github.com/OHDSI/Broadsea)
(Apache-2.0).
