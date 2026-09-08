import datetime as dt

import alert_queue


def test_failed_delivery_is_retried_and_not_dropped(tmp_path):
    db = tmp_path / "alerts.db"
    key = "2026-09-08:HK:00700:stop"
    now = dt.datetime(2026, 9, 8, 9, 30, tzinfo=dt.timezone.utc)
    assert alert_queue.enqueue(key, "stop event", 0, db_path=db, now=now) == "queued"
    assert alert_queue.flush(lambda _: False, db_path=db, now=now) == {
        "delivered": 0, "failed": 1, "attempted": 1,
    }
    assert alert_queue.status(key, db_path=db) == "retrying"

    # Before the exponential-backoff window expires, no duplicate is sent.
    assert alert_queue.flush(lambda _: True, db_path=db, now=now + dt.timedelta(seconds=59))["attempted"] == 0
    result = alert_queue.flush(lambda _: True, db_path=db, now=now + dt.timedelta(minutes=1))
    assert result == {"delivered": 1, "failed": 0, "attempted": 1}
    assert alert_queue.status(key, db_path=db) == "delivered"


def test_same_event_key_is_idempotent(tmp_path):
    db = tmp_path / "alerts.db"
    assert alert_queue.enqueue("key", "first", 1, db_path=db) == "queued"
    assert alert_queue.enqueue("key", "changed", 1, db_path=db) == "pending"
    delivered = []
    result = alert_queue.flush(lambda message: delivered.append(message) is None or True, db_path=db)
    assert result["delivered"] == 1
    assert delivered[-1] == "first"
