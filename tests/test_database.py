from database import db


def test_create_and_get_user():
    assert db.create_user(1, "alice", "Alice")
    user = db.get_user(1)
    assert user is not None
    assert user["username"] == "alice"
    assert user["user_id"] == 1


def test_get_all_user_ids():
    db.create_user(100, "bob", "Bob")
    assert 100 in db.get_all_user_ids()


def test_create_reminder_lifecycle():
    db.create_user(2, "bob", "Bob")
    rid = db.create_reminder(2, "custom", "Study", "Read 10 pages", "2026-10-01T08:00:00", False)
    assert rid is not None

    all_reminders = db.get_all_active_reminders()
    assert len(all_reminders) == 1
    assert all_reminders[0]["reminder_id"] == rid
    assert all_reminders[0]["message"] == "Read 10 pages"

    user_reminders = db.get_active_reminders(2)
    assert len(user_reminders) == 1
    assert db.get_active_reminders(999) == []


def test_recurring_reminder_flag():
    db.create_user(3, "carol", "Carol")
    db.create_reminder(3, "custom", "Daily", "Drink water", "2026-10-01T08:00:00", True, "daily")
    row = db.get_active_reminders(3)[0]
    assert row["is_recurring"] == 1


def test_deactivated_reminder_not_active():
    db.create_user(4, "dave", "Dave")
    rid = db.create_reminder(4, "custom", "One", "Once", "2026-10-01T08:00:00", False)
    conn = db._get_connection()
    conn.execute("UPDATE reminders SET is_active = 0 WHERE reminder_id = ?", (rid,))
    conn.commit()
    assert db.get_all_active_reminders() == []