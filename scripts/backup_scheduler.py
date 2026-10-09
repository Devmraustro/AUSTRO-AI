"""
AUSTRO AI - PostgreSQL backup scheduler (independent long-running worker).

Drives the one-shot job in `scripts/pg_backup.py` once a day at a fixed UTC
hour (default 02:00 UTC). It is designed to run as its OWN container/service
(or under a host supervisor), never inside the Telegram update loop, so backup
cost and failure cannot affect the bot's request handling.

Behaviour:
  * UTC scheduling is explicit: the next run is always computed in UTC and
    logged with an ISO-8601 UTC timestamp before sleeping.
  * Missed runs are recovered: on start-up, if today's scheduled hour has
    already passed and no successful backup exists for today (e.g. the container
    was down at 02:00), the backup runs immediately.
  * Duplicate runs are prevented by the lock file inside scripts/pg_backup.py.
  * A failed day is retried on a bounded backoff (BACKUP_RETRY_MINUTES, default
    60) instead of hot-looping against the database.
  * No restore drills are ever scheduled here; restore verification stays a
    separate monthly / per-release operator procedure (DISASTER_RECOVERY.md).
  * External alerting is out of scope: every run logs an explicit exit status
    and the worker keeps running, so `docker compose logs backup` (or the
    deployment's own scheduler) is the signal surface.

env: BACKUP_OUTDIR, BACKUP_SCHEDULE_UTC_HOUR, BACKUP_SCHEDULE_UTC_MINUTE,
     BACKUP_DAILY_KEEP, BACKUP_MONTHLY_KEEP, BACKUP_RETRY_MINUTES
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pg_backup import (  # noqa: E402
    DAILY_KEEP,
    MONTHLY_KEEP,
    encryption_required,
    list_backups,
    log,
    run_backup,
    utcnow,
)

DEFAULT_UTC_HOUR = 2
DEFAULT_UTC_MINUTE = 0
DEFAULT_RETRY_MINUTES = 60

_stopping = False


def _stop(signum, frame) -> None:  # pragma: no cover - signal path
    global _stopping
    _stopping = True
    log("scheduler", f"received signal {signum}; stopping after current step")


def next_run_utc(now: dt.datetime, hour: int = DEFAULT_UTC_HOUR,
                 minute: int = DEFAULT_UTC_MINUTE) -> dt.datetime:
    """The next strictly-future occurrence of hour:minute in UTC."""
    now = now.astimezone(dt.timezone.utc)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


def has_backup_for_date(outdir: Path, day: dt.date) -> bool:
    """True when a backup artifact already exists for that UTC calendar day."""
    for record in list_backups(Path(outdir)):
        if record.created_utc is not None and record.created_utc.date() == day:
            return True
    return False


def should_run_now(now: dt.datetime, outdir: Path,
                   hour: int = DEFAULT_UTC_HOUR,
                   minute: int = DEFAULT_UTC_MINUTE) -> bool:
    """Catch-up rule: run immediately when today's slot passed with no backup."""
    now = now.astimezone(dt.timezone.utc)
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= scheduled_today and not has_backup_for_date(outdir, now.date())


def _sleep_seconds(seconds: float) -> None:
    while not _stopping:
        remaining = seconds
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))
        seconds -= 30


def _sleep_until(target: dt.datetime) -> None:
    _sleep_seconds((target - utcnow()).total_seconds())


def run_scheduled_backup(outdir: Path, daily: int, monthly: int) -> int:
    """Run the one-shot backup and report its exit status (thin, testable seam)."""
    log("scheduler", "starting daily backup")
    status = run_backup(outdir, daily_keep=daily, monthly_keep=monthly)
    log("scheduler", f"backup run finished with exit={status}")
    return status


def main() -> int:
    # The scheduled worker is encrypted-only. Refuse before touching the disk or
    # starting the loop: a misconfigured container must never write plaintext.
    if not encryption_required():
        log("scheduler", "refusing to start: BACKUP_ENCRYPTION_ENABLED must be true "
                         "(the scheduled worker writes AES-256-GCM archives only)")
        return 2
    outdir = Path(os.environ.get("BACKUP_OUTDIR", "backups"))
    hour = int(os.environ.get("BACKUP_SCHEDULE_UTC_HOUR", DEFAULT_UTC_HOUR))
    minute = int(os.environ.get("BACKUP_SCHEDULE_UTC_MINUTE", DEFAULT_UTC_MINUTE))
    daily = int(os.environ.get("BACKUP_DAILY_KEEP", DAILY_KEEP))
    monthly = int(os.environ.get("BACKUP_MONTHLY_KEEP", MONTHLY_KEEP))
    retry_minutes = max(1, int(os.environ.get("BACKUP_RETRY_MINUTES",
                                              DEFAULT_RETRY_MINUTES)))
    outdir.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log("scheduler", (
        f"started; daily backup at {hour:02d}:{minute:02d} UTC "
        f"(now={utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}, outdir={outdir}, "
        f"retention daily={daily} monthly={monthly}, "
        f"retry={retry_minutes}min)"
    ))

    last_status = 0
    while not _stopping:
        now = utcnow()
        if should_run_now(now, outdir, hour, minute):
            log("scheduler", "scheduled slot reached and no backup exists for "
                             "today (normal run or missed-run recovery)")
        else:
            target = next_run_utc(now, hour, minute)
            log("scheduler", f"next scheduled run at {target.isoformat()} (UTC)")
            _sleep_until(target)
            if _stopping:
                break
            log("scheduler", "reached scheduled UTC time; starting backup")

        last_status = run_scheduled_backup(outdir, daily, monthly)
        if last_status != 0 and not _stopping:
            # A failed day must not turn into a hot retry loop against the
            # database; re-check after a bounded backoff instead.
            log("scheduler", f"backup did not succeed; backing off "
                             f"{retry_minutes} min before re-checking")
            _sleep_seconds(retry_minutes * 60)

    log("scheduler", "stopped")
    return last_status


if __name__ == "__main__":
    sys.exit(main())
