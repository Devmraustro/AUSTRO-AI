# AUSTRO AI - Monitoring & Alerting Strategy

## Overview

This document classifies monitoring capabilities into **IMPLEMENTED** (built into the application) and **EXTERNAL MONITORING REQUIRED** (requires external infrastructure).

---

## IMPLEMENTED: Application-Level Observability

### 1. Structured Logging (`app/observability/logging_config.py`)
- **Format**: JSON-structured logs with UTC timestamps
- **Redaction**: Automatic redaction of secrets (BOT_TOKEN, API keys, passwords, secrets)
- **Output**: Console + file (`logs/bot.log`) with UTF-8 encoding
- **Levels**: DEBUG, INFO, WARNING, ERROR (configurable via `AUSTRO_LOG_LEVEL`)

### 2. AI Telemetry (`app/ai/telemetry.py`)
Records per-request metrics:
- `run_id`: Unique request identifier
- `capability`: AI capability used (chat, coaching, tutoring, etc.)
- `category`: High-level category (personal_development, learning, etc.)
- `provider`: `gemini` or `local-fallback`
- `model`: Model name (e.g., `gemini-2.5-flash`)
- `latency_ms`: End-to-end latency
- `success`: Boolean success flag
- `retry_count`: Number of retries attempted
- `error`: Error class if failed
- `tokens_prompt` / `tokens_completion`: Token usage
- `estimated_cost_usd`: Estimated cost
- `request_id` / `nonce`: Request tracing

### 3. Error Taxonomy (`app/core/errors.py`)
Structured error classes with safe user messages:
- `ValidationError` - Input validation failures
- `AuthorizationError` - Permission denied
- `NotFoundError` - Resource not found
- `AIProviderError` - AI provider failures
- `DatabaseError` - Database errors
- `ExternalServiceError` - External service failures
- `RateLimitError` - Rate limit exceeded
- `ConfigurationError` - Invalid configuration

### 4. Error Handler (`app/telegram/main.py`)
```python
async def error_handler(update, context):
    logger.error(f"Update {update} caused error: {context.error}", exc_info=context.error)
    message = public_message(context.error)  # Safe user-facing message
    # Reply to user with safe message
```

---

## EXTERNAL MONITORING REQUIRED

The following monitoring capabilities **are not implemented** in the application and require external infrastructure:

| Capability | Status | Recommended Solution |
|------------|--------|---------------------|
| **Uptime Monitoring** | NOT IMPLEMENTED | UptimeRobot, Pingdom, Better Uptime, or custom healthcheck endpoint |
| **Alerting** | NOT IMPLEMENTED | PagerDuty, Opsgenie, Alertmanager, or custom webhook to Slack/Telegram |
| **Infrastructure Metrics** | NOT IMPLEMENTED | Prometheus + Grafana, Datadog, New Relic |
| **Database Monitoring** | NOT IMPLEMENTED | pg_stat_statements, pg_stat_activity, or pgAdmin |
| **Log Aggregation** | NOT IMPLEMENTED | ELK Stack, Loki, Datadog, Splunk |
| **Distributed Tracing** | NOT IMPLEMENTED | Jaeger, Zipkin, OpenTelemetry |
| **SLA Monitoring** | NOT IMPLEMENTED | Custom SLO tracking |

### Required External Setup

#### Minimal Production Setup
```yaml
# docker-compose.monitoring.yml
services:
  prometheus:
    image: prom/prometheus
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    ports:
      - "9090:9090"
  
  alertmanager:
    image: prom/alertmanager
    volumes:
      - ./alertmanager.yml:/etc/alertmanager/alertmanager.yml
    ports:
      - "9093:9093"
  
  grafana:
    image: grafana/grafana
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning
    ports:
      - "3000:3000"
```

#### Required Alerts
| Alert | Condition | Severity |
|-------|-----------|----------|
| `app_down` | Health check fails > 1min | Critical |
| `db_connection_failed` | DB connection errors > 5/min | Critical |
| `ai_provider_unavailable` | AI provider down > 5min | Warning |
| `high_error_rate` | Error rate > 5% | Warning |
| `high_latency` | P95 latency > 5s | Warning |
| `backup_failed` | Backup job fails | Warning |
| `disk_space_low` | Disk > 85% | Warning |
| `memory_high` | Memory > 85% | Warning |

---

## CLASSIFICATION

| Category | Status | Implementation |
|----------|--------|----------------|
| Structured Logging | ✅ IMPLEMENTED | `app/observability/logging_config.py` |
| AI Telemetry | ✅ IMPLEMENTED | `app/ai/telemetry.py` |
| Error Taxonomy | ✅ IMPLEMENTED | `app/core/errors.py` |
| Error Handling | ✅ IMPLEMENTED | `app/telegram/main.py` |
| Uptime Monitoring | ❌ EXTERNAL REQUIRED | UptimeRobot / custom |
| Alerting | ❌ EXTERNAL REQUIRED | Alertmanager / PagerDuty |
| Infrastructure Metrics | ❌ EXTERNAL REQUIRED | Prometheus + Grafana |
| Database Monitoring | ❌ EXTERNAL REQUIRED | pg_stat_statements |
| Log Aggregation | ❌ EXTERNAL REQUIRED | ELK / Loki |
| Distributed Tracing | ❌ EXTERNAL REQUIRED | Jaeger / Zipkin |

---

## RECOMMENDED MINIMAL PRODUCTION STACK

For minimal viable production monitoring:

1. **UptimeRobot** (free) - 50 monitors, 5-min intervals
2. **Prometheus + Grafana** (self-hosted) - metrics + dashboards
3. **Alertmanager** → Telegram/Slack webhook
4. **Loki** for log aggregation (optional)

### Quick Start Commands
```bash
# Start monitoring stack
docker-compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# Verify
curl http://localhost:9090/-/healthy  # Prometheus
curl http://localhost:9093/-/healthy  # Alertmanager
curl http://localhost:3000/api/health  # Grafana
```

---

## CONCLUSION

**Application-level observability: ✅ IMPLEMENTED**

**Production monitoring/alerting: ❌ EXTERNAL INFRASTRUCTURE REQUIRED**

Do not claim "monitoring complete" without deploying external monitoring infrastructure.