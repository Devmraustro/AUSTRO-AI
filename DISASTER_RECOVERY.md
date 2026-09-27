# AUSTRO AI — Disaster Recovery & PostgreSQL Backup/Restore Plan

Status: **DRILL-VERIFIED on real PostgreSQL 16.6** (2026-09-24).
Automation: daily 02:00 UTC backup worker **implemented and unit-tested**
(`Dockerfile.backup` + `backup` Compose service + `scripts/backup_scheduler.py`
+ `scripts/pg_backup.py`, 31 tests in `tests/test_backup.py`); **not yet
installed on production infrastructure** — item 10 stays open until an actual
scheduled run is observed on the host (§2.4).

This document defines the operational backup/restore and disaster-recovery
procedure. It was validated by a real `pg_dump` -> integrity check -> clean
restore -> schema/row-count -> application-reconnect drill.

---

## 1. Backup Design

| Property | Value |
|---|---|
| Backup tool | PostgreSQL `pg_dump` (logical, plain SQL, `--clean --if-exists --no-owner --no-acl`) |
| pg_dump client | `postgresql-client-16` in `Dockerfile.backup` (must match the server major version) |
| Authentication | `0600` `.pgpass` via `PGPASSFILE` + `--no-password`; the password never appears in argv, logs, or the manifest |
| Compression | gzip (level 6), streamed so memory stays flat on large dumps |
| Integrity | SHA-256 recorded in a JSON manifest next to each archive: `checksum_sha256` (decompressed SQL, used by the restore drill) and `archive_sha256` (gzip file) |
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
austro_ai_backup_<UTCTIMESTAMP>.sql.gz
austro_ai_backup_<UTCTIMESTAMP>.manifest.json
```

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
> database. Only ever point it at an explicitly disposable/probe database.

```bash
# Purposely destructive: drops and recreates <DB_NAME> from the backup.
python scripts/pg_restore_drill.py /var/backups/austro_ai_backup_<TS>.sql.gz \
    /var/backups/austro_ai_backup_<TS>.manifest.json
```

The restore script:
1. Verifies archive checksum against the manifest.
2. Applies the SQL to a **disposable probe database** first to prove the backup
   parses cleanly (`ON_ERROR_STOP=1`).
3. Drops and recreates the target database (`DROP DATABASE ... WITH (FORCE)`).
4. Restores the SQL.
5. Validates: table set == manifest table set, per-table row counts == manifest
   row counts.
6. Reconnects through the application `DatabaseManager` and confirms queries.

Exit code 0 = restored and verified.

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
- Encryption at rest for backup archives (age/KMS).
- Off-host copy of the `backups` volume (the volume is persistent but still
  local to the host).
