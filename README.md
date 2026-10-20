# Aura 0.1

Aura is a local companion module for a NAS photo library. It keeps the NAS
files read-only, scans them through a mounted SMB path, and stores the data
needed for people, events, tags, and smart-album rules in its own database.

The intended deployment is:

```text
NAS (SMB, read-only)  <-- LAN -->  sidecar host
                                   ├─ Immich (photo UI, installed separately)
                                   └─ Aura (this repository)
```

The 2-core/2GB NAS should remain a storage provider. Aura is designed to run
on the stronger sidecar host and can later be connected to Immich through its
HTTP API. The first version intentionally does not write to image files,
delete originals, or depend on Immich's private database schema.

## 0.1 capabilities

- recursive, idempotent indexing of common image formats
- EXIF capture time and GPS extraction when available
- a local SQLite database with migration-friendly tables for assets, people,
  tags, events, and smart albums
- structured search over indexed metadata
- a small HTTP interface for the companion UI and future Immich adapters
- dry-run and scheduled scans
- Docker Compose deployment with a read-only photo mount

Face embeddings, scene tags, event clustering, and Immich API materialization
are deliberately represented as explicit extension points. They can be added
without changing the storage interface or moving the original files.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
PHOTO_ROOT=/path/to/photos AURA_DB_PATH=./data/aura.db \
  python -m aura.cli scan
uvicorn aura.app:app --reload
```

Then open `http://127.0.0.1:8080/docs` or query `GET /health`.

## Docker

Copy `.env.example` to `.env`, set the NAS SMB values, and run:

```bash
docker compose -f docker-compose.yml -f docker-compose.smb.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.smb.yml \
  --profile jobs run --rm aura-scan
docker compose -f docker-compose.yml -f docker-compose.smb.yml \
  --profile jobs run --rm aura-scan events
```

The recommended `docker-compose.smb.yml` override lets Docker mount SMB
directly, without a host-level mount. If you choose to mount SMB yourself on
the host, set `PHOTO_ROOT` in `.env` and run the base `docker-compose.yml`
without the override.

This requires only the NAS address, share name, username, and password in
`.env`. Docker's local volume driver performs the CIFS mount and the container
still receives `/photos` read-only. The password is visible to the Docker
daemon as a volume option, so use a dedicated read-only NAS account and never
commit `.env`.

The sidecar host must have the CIFS mount helper installed. On Debian or
Ubuntu, run `sudo apt install cifs-utils` once. If the password contains `$`,
put the value in single quotes in `.env`, for example
`NAS_PASSWORD='abc$ZUxyz'`; Compose treats single-quoted values literally.

For a nightly scan, install `scripts/nightly-scan.sh` as a host cron job after
the SMB mount is configured. The script uses the `aura-scan` Compose profile,
so the web process is not restarted while indexing runs.

## Scan and search interfaces

```text
GET  /health
POST /api/v1/scans       {"dry_run": false}
GET  /api/v1/assets?q=beach&start=2024-01-01&end=2025-01-01
GET  /api/v1/search/parse?text=去年和张三的旅行照片
GET  /api/v1/people
GET  /api/v1/events
GET  /api/v1/tags
POST /api/v1/people
POST /api/v1/tags
POST /api/v1/events
POST /api/v1/events/candidates
POST /api/v1/smart-albums
```

The API is intentionally small. The companion Web UI and an Immich adapter
should depend on these interfaces rather than on SQLite tables directly.

## 0.1 limits

The scanner currently indexes common raster images and EXIF metadata. Face
embeddings, automatic scene tags, event clustering, Chinese query parsing, and
materializing albums into Immich are the next adapters. Duplicate detection is
optional and intentionally does not delete files. Metadata is kept in Aura's
database; the SMB source remains read-only.
