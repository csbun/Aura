# Aura roadmap

This roadmap turns the current `0.1` scanner into a maintainable local photo
organizer. The source of truth remains the read-only NAS library; Aura owns
derived metadata and review decisions. Immich remains a replaceable display
adapter rather than a database that Aura edits directly.

## 0.1 — complete

- SMB/CIFS Docker volume and read-only library access
- idempotent image indexing with SHA-256 and EXIF/GPS metadata
- SQLite schema for assets, people, tags, events, and smart albums
- deterministic day-level event candidates
- small HTTP interface and Chinese query-plan endpoint
- nightly scan command and progress logging

## 0.2 — integration and review loop

1. Add external-identity mappings so a local asset can map to an Immich asset
   without storing Immich UUIDs in the core asset table.
2. Add an Immich adapter with health checks, album discovery, album creation,
   and idempotent asset membership updates. Keep it opt-in and API-key based.
3. Add a review API for confirming or rejecting event candidates and generated
   tags. Confirmed records must survive regeneration.
4. Add a small companion Web UI for scan status, events, people, tags, and
   smart-album rules.

Acceptance: an operator can connect an existing Immich instance, materialize
one confirmed event as an album, run the operation twice without duplicates,
and continue browsing photos if Immich is unavailable.

## 0.3 — people and tags

1. Run a CPU-friendly face detector/embedding worker on the sidecar.
2. Store model name, model version, confidence, and review state for every
   face assignment.
3. Add a controlled system-tag vocabulary plus user tags.
4. Make model jobs resumable and bounded by a one-worker CPU budget.

Acceptance: unknown faces can be merged, split, named, and reprocessed without
changing original files.

## 0.4 — events and search

1. Add GPS clustering and place normalization to the deterministic time
   grouping.
2. Add scene/OCR embeddings behind a replaceable model adapter.
3. Expand the Chinese parser into a reviewable structured query plan.
4. Support people + event + tag + time + place smart-album rules.

Acceptance: a query can be previewed as structured filters, corrected by the
user, and materialized as a dynamic Immich album.

## 0.5 — hardening

- resource limits and queue visibility
- database backup/restore checks
- SMB reconnect and stale-file handling
- pinned Immich compatibility matrix
- metrics for indexing throughput, failures, and model backlog
- optional duplicate detection; never automatic deletion

## Decisions that stay fixed

- originals remain on NAS and are never rewritten by Aura
- network access is LAN-only by default
- Postgres/SQLite and derived indexes live on the sidecar's local disk
- model processing is offline after model installation
- external integrations use adapters and stable Aura interfaces
