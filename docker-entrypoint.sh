#!/bin/bash
set -e

echo "Starting AUSTRO AI..."

# Wait for the database to be ready (PostgreSQL architecture B, or SQLite local)
if [ "$DB_ENGINE" = "postgresql" ]; then
    echo "Waiting for PostgreSQL at $DB_HOST:$DB_PORT..."
    while ! pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" > /dev/null 2>&1; do
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