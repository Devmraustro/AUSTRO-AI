#!/bin/bash
# PostgreSQL Backup Script for AUSTRO AI
# Usage: ./backup.sh [backup_name]

set -e

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
DB_NAME="${DB_NAME:-austro_ai}"
DB_USER="${DB_USER:-austro}"
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_NAME="${1:-backup_${TIMESTAMP}}"
BACKUP_FILE="${BACKUP_DIR}/${BACKUP_NAME}.sql.gz"

echo "Starting backup: ${BACKUP_NAME}"

# Create backup directory
mkdir -p "${BACKUP_DIR}"

# Create backup
pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
    --no-owner --no-acl --clean --if-exists \
    | gzip > "${BACKUP_FILE}"

# Verify backup
if [ -f "${BACKUP_FILE}" ] && [ -s "${BACKUP_FILE}" ]; then
    SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
    echo "Backup completed: ${BACKUP_FILE} (${SIZE})"
    
    # Verify backup integrity
    echo "Verifying backup integrity..."
    if gunzip -t "${BACKUP_FILE}"; then
        echo "Backup integrity verified"
    else
        echo "ERROR: Backup integrity check failed"
        exit 1
    fi
else
    echo "ERROR: Backup file not created or empty"
    exit 1
fi

# Cleanup old backups (keep last 7 days)
find "${BACKUP_DIR}" -name "backup_*.sql.gz" -mtime +7 -delete

echo "Backup completed successfully: ${BACKUP_FILE}"