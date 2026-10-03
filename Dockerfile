# AUSTRO AI - Production Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
# postgresql-client provides `pg_isready`, which docker-entrypoint.sh uses to
# wait for the database before applying migrations. Without it the wait loop
# spins forever on "command not found" and the container never starts.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
#
# This was `COPY . .`, which put the entire repository into the image. With no
# `.dockerignore` (the audit found none tracked) that meant local `.env` files,
# SQLite databases, `backups/*.sql` (full memory + knowledge content), logs and
# `.git` all became image layers - readable by anyone who could pull the image,
# even though a later layer deleted them. `.gitignore` has no effect on the
# build context, so this file is the actual control.
#
# An explicit allow-list is used instead of `COPY . .` so that a new secret or
# runtime artefact is never included by default. Anything not copied here is
# absent from the image, and `tests/dockerignore_check.py` fails CI if a runtime
# path is missing.
COPY app ./app
COPY scripts ./scripts
COPY main.py handlers.py config.py database.py reminder_scheduler.py \
     redaction.py smoke_test.py ./

# Create non-root user
RUN useradd --create-home --shell /bin/bash austro
RUN chown -R austro:austro /app

# Startup script. This MUST happen before `USER austro`: `--chmod` is applied by
# the builder as root, whereas a later `RUN chmod +x` executes as `austro` and
# fails with "Operation not permitted" on this root-owned file.
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/

USER austro

# Health check: config + database reachable (readiness)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/healthcheck.py

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py"]