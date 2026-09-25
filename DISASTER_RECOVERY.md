# AUSTRO AI — Disaster Recovery & PostgreSQL Backup/Restore Plan

Status: **DRILL-VERIFIED on real PostgreSQL 16.6** (2026-09-24).

This document defines the operational backup/restore and disaster-recovery
procedure. It was validated by a real `pg_dump` -> integrity check -> clean
restore -> schema/row-count -> application-reconnect drill.

---

## 1. Backup Design

| Property | Value |
|---|---|
| Backup tool | PostgreSQL `pg_dump` (logical, plain SQL, `--clean --if-exists --no-owner --no-acl`) |
| Compression | gzip (level 6) |
| Integrity | SHA-256 checksum recorded in a JSON manifest next to each archive |
| Row-count manifest | per-table row counts captured at backup time |
| Frequency | Daily 02:00 UTC (automated) + manual on-demand before migrations/releases |
| Retention | Keep last 7 daily backups (default); 30 for monthly archive |
| Storage | `backups/` volume, off-host copy (S3/bucket) recommended for real DR |
| Verification | nightly `gunzip -t` + checksum check; monthly full restore drill |

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

```bash
# Automated (cron / scheduled task): daily
python scripts/pg_backup.py /var/backups/austro

# Outputs:
#   /var/backups/austro_ai_backup_<UTCTIMESTAMP>.sql.gz
#   /var/backups/austro_ai_backup_<UTCTIMESTAMP>.manifest.json
```

Requirements: `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` environment and
`PG_BIN` pointing at the PostgreSQL `bin` directory.

Exit code 0 = success. The script:
1. Snapshots per-table row counts.
2. Runs `pg_dump` (plain SQL, UTF-8).
3. gzips + writes SHA-256 manifest.
4. Verifies gzip de-archive + checksum round-trip before reporting success.

## 3. Restore Procedure (commands)

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
- Prometheus/alertmanager alert: `backup_failed` if the daily job fails.
- Encryption at rest for backup archives (age/KMS).