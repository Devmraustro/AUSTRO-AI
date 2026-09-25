from database import db
from reminder_scheduler import ReminderScheduler


class FakeJob:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs_list = []

    def jobs(self):
        return list(self.jobs_list)

    def _mk(self, name, **kwargs):
        job = FakeJob(name, **kwargs)
        self.jobs_list.append(job)
        return job

    def run_daily(self, callback, time=None, name=None, data=None):
        return self._mk(name, time=time, data=data)

    def run_once(self, callback, when=None, name=None, data=None):
        return self._mk(name, when=when, data=data)

    def run_repeating(self, callback, interval=None, first=None, name=None):
        return self._mk(name, interval=interval, first=first)


class FakeApp:
    def __init__(self):
        self.job_queue = FakeJobQueue()
        self.bot_data = {}


async def test_resync_schedules_one_off_and_recurring():
    db.create_user(10, "u", "U")
    one_off = db.create_reminder(10, "custom", "t", "m", "2026-10-01T08:00:00", False)
    recurring = db.create_reminder(10, "custom", "t2", "m2", "2026-10-02T09:00:00", True)

    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()

    jobs = scheduler.application.job_queue.jobs_list
    by_name = {job.name: job for job in jobs}
    assert f"custom_reminder_{one_off}" in by_name
    assert f"custom_reminder_{recurring}" in by_name
    assert "when" in by_name[f"custom_reminder_{one_off}"].kwargs
    assert "time" in by_name[f"custom_reminder_{recurring}"].kwargs


async def test_resync_removes_stale_jobs():
    db.create_user(11, "u", "U")
    rid = db.create_reminder(11, "custom", "t", "m", "2026-10-01T08:00:00", False)

    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()

    job = scheduler.application.job_queue.jobs_list[0]
    assert not job.removed

    conn = db._get_connection()
    conn.execute("UPDATE reminders SET is_active = 0 WHERE reminder_id = ?", (rid,))
    conn.commit()

    await scheduler.resync()
    assert job.removed


async def test_resync_skips_duplicate_jobs():
    db.create_user(12, "u", "U")
    rid = db.create_reminder(12, "custom", "t", "m", "2026-10-01T08:00:00", False)

    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = True
    await scheduler.resync()
    await scheduler.resync()

    names = [job.name for job in scheduler.application.job_queue.jobs_list]
    assert names.count(f"custom_reminder_{rid}") == 1


async def test_resync_noop_when_not_running():
    db.create_user(13, "u", "U")
    db.create_reminder(13, "custom", "t", "m", "2026-10-01T08:00:00", False)

    scheduler = ReminderScheduler(FakeApp())
    scheduler.running = False
    await scheduler.resync()
    assert scheduler.application.job_queue.jobs_list == []