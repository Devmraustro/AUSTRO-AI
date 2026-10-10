# AUSTRO AI — Disaster Recovery & PostgreSQL Backup/Restore Plan

Status: **encrypted-only backup worker and fail-closed restore drill
implemented and unit-tested** (`tests/test_backup.py`: 59 tests,
`tests/test_restore_safety.py`: 33 tests). The end-to-end proof (encrypted
backup, wrong-key refusal, tampered-archive refusal, unsafe-target refusal,
correct-key restore) runs in the GitHub Actions job `backup-integration` against
an ephemeral PostgreSQL 16 service with a runner-generated 32-byte key. §7 records
that run. **Not yet installed on production infrastructure**: item 10 stays open
until a scheduled run is observed on the host (§2.4).

This document defines the operational backup/restore and disaster-recovery
procedure. The earlier plaintext drill (2026-09-24, PostgreSQL 16.6) is
superseded: archives are now always encrypted, and the restore path has the
safety gates described in §3.

---

## 1. Backup Design

| Property | Value |
|---|---|
| Backup tool | PostgreSQL `pg_dump` (logical, plain SQL, `--clean --if-exists --no-owner --no-acl`), piped through gzip into an AES-256-GCM container. The plain SQL dump lives only in a private 0700 work directory and is deleted on success, failure and stale-run recovery. It never reaches the shared backup volume. |
| pg_dump client | `postgresql-client-16` in `Dockerfile.backup` (must match the server major version) |
| Authentication | `0600` `.pgpass` via `PGPASSFILE` + `--no-password`; the password never appears in argv, logs, or the manifest |
| Encryption | **Always on in the worker** (`BACKUP_ENCRYPTION_ENABLED=true`). AES-256-GCM, key = raw 32-byte file at `BACKUP_ENCRYPTION_KEY_FILE` (a Compose secret). A missing or wrong-sized key stops the run before any dump. Container layout: `0x01 \|\| nonce(12) \|\| ciphertext \|\| tag(16)`. The scheduler refuses to start with the flag off. |
| Compression | gzip (level 6), streamed so memory stays flat on large dumps |
| Integrity | SHA-256 recorded in a JSON manifest next to each archive: `checksum_sha256` (decompressed SQL) and `archive_sha256` (the published, encrypted file). The manifest also records `encryption.enabled` and `encryption.algo = aes-256-gcm`. |
| Row-count manifest | per-table row counts captured at backup time |
| Frequency | Daily **02:00 UTC** by the independent `backup` container; manual on-demand before migrations/releases |
| Scheduling | `scripts/backup_scheduler.py` (separate container/service) — **never** inside the Telegram update loop |
| Missed-run recovery | if the container starts after 02:00 UTC and no backup exists for the current UTC date, it runs immediately |
| Duplicate-run protection | exclusive `.backup.lock`; a stale lock left by a killed run is reclaimed after 1 hour |
| Retention | newest **7** backups (daily window) + the newest backup of each UTC calendar month for up to **30** further months |
| Storage | `backups` Docker volume (persistent), off-host copy (S3/bucket) recommended for real DR |
| Verification | gzip round-trip + both SHA-256 digests checked **before** success; failures delete the new pair and never touch older backups |

### RPO (Recovery Point Objective)
- Max data loss window: **24 hours** (daily backups) — reduced to near-zero if
  `archive_mode=on` + WAL archiving is enabled (see "future work").
- During the drill the RPO is the time between seed and backup snapshot.

### RTO (Recovery Time Objective)
- Target: **<= 30 minutes** from incident start to serving traffic.
- Measured restore: fresh database created + full SQL applied + application
  reconnect validated. The drill completed in well under the target on a local
  instance (rebuild time dominated by data volume).

## 2. Backup Procedure (commands)

### 2.1 Automated (production)

The backup runs as its **own** Compose service so its cost and failures can
never affect the bot:

```bash
# start the scheduler (plus the app) on the production host
docker compose --env-file .env up -d app backup

# follow the schedule
docker compose logs -f backup

# is the worker alive and what did it do today?
docker compose ps backup
docker compose exec backup ls -l /app/backups
```

Configuration (all optional, defaults in parentheses):
`BACKUP_SCHEDULE_UTC_HOUR` (2), `BACKUP_SCHEDULE_UTC_MINUTE` (0),
`BACKUP_DAILY_KEEP` (7), `BACKUP_MONTHLY_KEEP` (30),
`BACKUP_RETRY_MINUTES` (60 — how long the worker waits before re-checking a
day that failed, so a broken job cannot hot-loop against the database).

`DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` must be the **same externally
managed production database** the app uses. The image pins
`postgresql-client-16`, so `pg_dump` matches the PostgreSQL 16 server.

> Starting the service is **not** proof that a backup ran. Verify the first
> real scheduled run as in §2.4 before treating item 10 of the release
> checklist as operationally complete.

### 2.2 Manual one-shot

```bash
# inside the backup container
docker compose run --rm backup python scripts/pg_backup.py /app/backups

# on a host with a local PostgreSQL install
python scripts/pg_backup.py /var/backups/austro
```

Outputs (same layout either way):
```
austro_ai_backup_<UTCTIMESTAMP>.sql.gz            # AES-256-GCM container when a key is configured
austro_ai_backup_<UTCTIMESTAMP>.manifest.json
```
The `.sql.gz` name is kept for compatibility. With a key configured, the file is
the encrypted container above. Without one (only possible when
`BACKUP_ENCRYPTION_ENABLED` is unset, i.e. a manual local run), it is plain gzip.
The production worker never runs in that mode.

Requirements: `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` and
`PG_BIN` pointing at the PostgreSQL `bin` directory (not needed in the
container image, which ships the client).

Exit code 0 = success. The script:
1. Takes an exclusive lock (skipping cleanly if another run holds it).
2. Snapshots per-table row counts.
3. Runs `pg_dump` (plain SQL, UTF-8) with credentials from `PGPASSFILE`.
4. Streams it through gzip and writes the SHA-256 manifest to `.part` files.
5. Publishes archive+manifest atomically, then re-verifies the published pair.
6. Only then applies retention.

On any failure the new (or newly published, unverified) archive **and** its
manifest are removed together, every earlier backup is left untouched, and the
exit code is non-zero. A half-written archive is never counted as a recovery
point.

### 2.3 Retention rule (exact)

1. The newest **7** complete backups are always kept (the daily window).
2. Walking the rest newest-first, the newest backup of each UTC calendar month
   is kept as that month's recovery point, for at most **30** additional months.
   A month already represented inside the daily window adds no duplicate point.
3. Archives and manifests are deleted only in pairs; incomplete artifacts and
   files whose timestamp cannot be parsed are never auto-deleted (they are
   logged as "incomplete artifact left untouched" for manual review).
4. Pruning only runs after the new backup has been published **and** verified,
   so a failed run can never delete a known-good recovery point. The newest
   backup is never deleted.

### 2.4 Verifying that the automated backup really works

```bash
docker compose logs backup | tail -40        # "6/6: SUCCESS backup=..." at ~02:00 UTC
ls -l /var/lib/docker/volumes/*backups*/_data   # archive + manifest with today's stamp
```

Then prove it restorably — **always against a disposable database** (§3).

### 2.5 Monthly / pre-release restore drill

Restore drills are deliberately **not** scheduled by the backup worker: they
are destructive and operator-supervised. Run §3 monthly and before every
release. The nightly job only takes a backup and verifies its own integrity.

## 3. Restore Procedure (commands)

> **Destructive.** `pg_restore_drill.py` drops and recreates the target
> database. It refuses to run unless **all** of the following hold, and it
> refuses before opening any archive or database connection (exit 2):
>
> - `RESTORE_TARGET_DB` is set explicitly. There is **no default**, so a restore
>   never falls back to the application database. The name must contain
>   `restore` and must differ from `DB_NAME`.
> - `RESTORE_CONFIRM_DESTRUCTIVE=DROP-AND-RESTORE:<RESTORE_TARGET_DB>` matches
>   the target exactly.
> - `RESTORE_ADMIN_USER` and `RESTORE_ADMIN_PASSWORD` are set. The drill never
>   assumes a `postgres` superuser.

```bash
RESTORE_TARGET_DB=austro_ai_restore_drill \
RESTORE_CONFIRM_DESTRUCTIVE=DROP-AND-RESTORE:austro_ai_restore_drill \
RESTORE_ADMIN_USER=<admin> RESTORE_ADMIN_PASSWORD=<from secret store> \
DB_NAME=<application db> DB_USER=<application role> DB_PASSWORD=<app password> \
BACKUP_ENCRYPTION_KEY_FILE=/run/secrets/backup_encryption_key \
python scripts/pg_restore_drill.py /var/backups/austro_ai_backup_<TS>.sql.gz \
    /var/backups/austro_ai_backup_<TS>.manifest.json
```

Steps, in order. Each step fails closed before the next:
1. Safety rules above (exit 2 on refusal, nothing touched).
2. **Authenticate the archive before any database work**: manifest and archive
   checksums, key length and decryption (AES-GCM authentication), decompression,
   and the content SHA-256 against the manifest. A wrong key or a single
   tampered byte stops the drill here (exit 1). The target database is
   unchanged, and the log says so.
3. Drop and recreate the target as the administrative role
   (`DROP DATABASE ... WITH (FORCE)`, then create, owner `DB_USER`).
4. Stream the verified SQL into the target with `psql -v ON_ERROR_STOP=1` as the
   administrative role, and grant the application role access when it differs.
5. Validate the table set, per-table row counts, and connectivity as the
   application role, against the manifest.

Exit codes: **0** restored and verified; **1** failed (for authentication
failures no database was changed); **2** refused by a safety rule before any
archive or database work.

## 4. Application Reconnect

Validated in the drill:
```
app manager query OK: tables=44, users=2   ->  users match manifest
```
After a restore the application picks the restored database automatically
(connection settings are shared). No application restart procedure beyond
normal startup is required.

## 5. SQLite note

SQLite file-copy backups remain valid for the local/dev engine, but they are
**not** counted as PostgreSQL disaster recovery. PostgreSQL DR uses the verified
`pg_dump`/`pg_restore` drill above.

## 6. Future improvements (not yet implemented)
- `archive_mode=on` + continuous WAL archiving to reduce RPO to minutes.
- Off-host/managed backup storage with point-in-time recovery.
- Prometheus/alertmanager alert: `backup_failed` if the daily job fails. The
  worker currently only logs a non-zero exit signal; alerting is left to the
  deployment's own scheduler/monitoring.
- Off-host copy of the `backups` volume (the volume is persistent but still
  local to the host).
- Key management beyond a file secret (KMS/age-wrapped keys, rotation). The key
  is currently a single 32-byte file; rotating it means writing new backups
  with the new key and keeping the old key until older archives expire.

## 7. CI evidence (GitHub Actions `backup-integration`)

Code under test: commit `0972a57` on `arena/d94e58c6-austro-ai` (documentation
commits after it do not change any code path).

| Run | Event | Result |
|---|---|---|
| [38006850038](https://github.com/Devmraustro/AUSTRO-AI/actions/runs/38006850038) | push | all 9 jobs success |
| [38006853850](https://github.com/Devmraustro/AUSTRO-AI/actions/runs/38006853850) | pull_request | all 9 jobs success |

Steps of job `Encrypted backup and disposable restore (PostgreSQL 16)` in run
38006850038 (each step's conclusion is `success`):

1. Ephemeral secrets generated on the runner (32-byte backup key, separate wrong key; values masked and never printed)
2. Real backup image built from `Dockerfile.backup`; `cryptography` (AESGCM) and `psycopg2` imported in the image
3. Disposable source database, restore target and application role seeded
4. Encrypted backup: `pg_dump` 16 -> AES-256-GCM, published pair verified on the runner (no plaintext SQL markers, manifest declares `aes-256-gcm`, no key material)
5. Wrong key refused with exit 1, target sentinel data intact
6. Tampered archive (one flipped byte) refused with exit 1, target sentinel data intact
7. Unsafe targets (application DB) and missing destructive confirmation refused with exit 2
8. Restore into the disposable database with the correct key: exit 0, restored counts verified against the manifest
9. Ephemeral keys and working files removed (`if: always()`)

The job's raw log could not be downloaded in this session (the log service returned
errors), so the table records step-level conclusions from the GitHub API.

Local rehearsal before the push, against PostgreSQL 16.2, using the same scripts
and the PATH-only tool lookup: backup published and verified (3 tables, 15 rows);
wrong key exit 1 with "NOT modified"; correct-key restore exit 0, `tables=3
total_rows=15 ALL ROW COUNTS MATCH`, `RESTORE DRILL: ALL CHECKS PASSED`.

