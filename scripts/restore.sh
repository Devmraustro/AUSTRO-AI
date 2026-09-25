#!/bin/bash
# PostgreSQL Restore Script for AUSTRO AI
# Usage: ./restore.sh <backup_file>

set -e

BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
DB_NAME="${DB_NAME:-austro_ai}"
DB_USER="${DB_USER:-austro}"
DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"

if [ $# -eq 0 ]; then
    echo "Usage: $0 <backup_file>"
    echo "Available backups:"
    ls -la "${BACKUP_DIR}"/*.sql.gz 2>/dev/null || echo "No backups found"
    exit 1
fi

BACKUP_FILE="${BACKUP_DIR}/${1}"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "ERROR: Backup file not found: ${BACKUP_FILE}"
    echo "Available backups:"
    ls -la "${BACKUP_DIR}"/*.sql.gz 2>/dev/null || echo "No backups found"
    exit 1
fi

echo "Restoring from: ${BACKUP_FILE}"
echo "Target database: ${DB_NAME} on ${DB_HOST}"

# Verify backup integrity
echo "Verifying backup integrity..."
if ! gunzip -t "${BACKUP_FILE}"; then
    echo "ERROR: Backup integrity check failed"
    exit 1
fi

# Confirm restore
echo "WARNING: This will replace all data in database '${DB_NAME}'"
read -p "Continue? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Restore cancelled"
    exit 1
fi

# Drop and recreate database
echo "Dropping and recreating database..."
psql -h "${DB_HOST}" -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME}; CREATE DATABASE ${DB_NAME};"

# Restore
echo "Restoring database..."
gunzip -c "${BACKUP_FILE}" | psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}"

# Verify restore
echo "Verifying restore..."
TABLE_COUNT=$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")
echo "Tables restored: ${TABLE_COUNT}"

# Verify row counts for key tables
for table in users goals habits learning_mastery memories knowledge_sources; do
    COUNT=$(psql -h "${DB_HOST}" -U "${DB_USER}" -d "${DB_NAME}" -t -c "SELECT count(*) FROM ${table};" 2>/dev/null || echo "0")
    echo "  ${table}: ${COUNT} rows"
done

echo "Restore completed successfully"