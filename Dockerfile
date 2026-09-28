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

# Copy application code
COPY . .

# Create non-root user
RUN useradd --create-home --shell /bin/bash austro
RUN chown -R austro:austro /app
USER austro

# Health check: config + database reachable (readiness)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/healthcheck.py

# Startup script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py"]