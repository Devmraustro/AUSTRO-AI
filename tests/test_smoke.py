import main as app_module


def test_application_builds():
    app = app_module.build_application()
    assert app.post_init is not None
    assert app.post_stop is not None
    assert len(app.handlers) >= 1


def test_scheduler_wired_via_post_init():
    assert app_module.scheduler_post_init is not None
    assert app_module.scheduler_post_stop is not None


async def test_scheduler_post_init_starts_jobs():
    app = app_module.build_application()
    assert app.job_queue is not None
    await app_module.scheduler_post_init(app)
    names = [job.name for job in app.job_queue.jobs()]
    assert "reminder_checker" in names
    assert "morning_reminder" in names
    assert "noon_reminder" in names
    assert "evening_reminder" in names
    assert "night_reminder" in names
    assert "motivation_reminder" in names