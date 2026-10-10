#!/bin/bash
# DEPRECATED WRAPPER - kept only so old runbooks do not silently misbehave.
#
# This script used to write an UNENCRYPTED `pg_dump | gzip` into the backup
# directory. It now delegates to the encrypted, verified one-shot backup,
# which fails closed unless BACKUP_ENCRYPTION_ENABLED / BACKUP_ENCRYPTION_KEY_FILE
# are configured. Usage: scripts/backup.sh [outdir]
set -euo pipefail
cd "$(dirname "$0")/.."
# Force encryption on: this wrapper must never produce a plaintext archive, even
# when the operator's environment leaves BACKUP_ENCRYPTION_ENABLED unset.
export BACKUP_ENCRYPTION_ENABLED=true
exec python3 scripts/pg_backup.py "${1:-${BACKUP_OUTDIR:-backups}}"
