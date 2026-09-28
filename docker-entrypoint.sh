#!/bin/bash
set -e

echo "Starting AUSTRO AI..."

# Wait for the database to be ready (PostgreSQL architecture B, or SQLite local)
if [ "$DB_ENGINE" = "postgresql" ]; then
    # Bounded wait: a wrong DB_HOST/DB_PORT or a missing client binary must fail
    # loudly instead of hanging the container forever in a restart loop.
    DB_WAIT_TIMEOUT_SECONDS="${DB_WAIT_TIMEOUT_SECONDS:-120}"
    if ! command -v pg_isready > /dev/null 2>&1; then
        echo "FATAL: pg_isready not found in this image (install postgresql-client)."
        exit 1
    fi
    echo "Waiting up to ${DB_WAIT_TIMEOUT_SECONDS}s for PostgreSQL at $DB_HOST:$DB_PORT..."
    DEADLINE=$(( $(date +%s) + DB_WAIT_TIMEOUT_SECONDS ))
    until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" > /dev/null 2>&1; do
        if [ "$(date +%s)" -ge "$DEADLINE" ]; then
            echo "FATAL: PostgreSQL at $DB_HOST:$DB_PORT not ready after ${DB_WAIT_TIMEOUT_SECONDS}s."
            echo "       Check DB_HOST/DB_PORT/DB_USER/DB_NAME and that the server accepts connections."
            exit 1
        fi
        echo "Waiting for PostgreSQL..."
        sleep 2
    done
    echo "PostgreSQL is ready"
fi

# Run migrations / schema initialization (engine-aware)
echo "Running database migrations..."
python -c "
import sys
sys.path.insert(0, '.')
from app.database.connection import DatabaseManager
from app.database.migrations import apply_migrations

db = DatabaseManager()
apply_migrations(db._get_connection())
print('Schema + migrations applied successfully')
"

echo "Starting application..."
exec python main.py
