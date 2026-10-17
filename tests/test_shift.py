from datetime import datetime, timezone

from engine import db as dbm
from engine import shift
from engine import shifts as shifts_mod
from tests.helpers import connect, dt

UTC = timezone.utc


def test_upsert_shift_inserts(tmp_path):
    conn = connect(tmp_path)
    sid = shift.upsert_shift(conn, 111, "Asia/Jakarta", "09:00", "17:00")
    row = dbm.q1(conn, "SELECT * FROM shifts WHERE id=?", (sid,))
    assert row["admin_tg_user_id"] == 111
    assert row["tz"] == "Asia/Jakarta"
    assert row["start_local"] == "09:00"
    assert row["days_mask"] == "1111111"


def test_upsert_shift_updates_same_admin(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00")
    shift.upsert_shift(conn, 111, "UTC", "18:00", "23:00")
    rows = dbm.q(conn, "SELECT * FROM shifts WHERE admin_tg_user_id=111")
    assert len(rows) == 1
    assert rows[0]["start_local"] == "18:00"


def test_upsert_rejects_bad_tz(tmp_path):
    conn = connect(tmp_path)
    try:
        shift.upsert_shift(conn, 111, "Mars/Olympus", "09:00", "17:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_upsert_rejects_bad_time(tmp_path):
    conn = connect(tmp_path)
    for bad in ("25:00", "9", "09:99", "abc"):
        try:
            shift.upsert_shift(conn, 111, "UTC", bad, "17:00")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_upsert_rejects_identical_start_end(tmp_path):
    conn = connect(tmp_path)
    try:
        shift.upsert_shift(conn, 111, "UTC", "09:00", "09:00")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_upsert_rejects_bad_days_mask(tmp_path):
    conn = connect(tmp_path)
    for bad in ("111", "11111112", "111111x"):
        try:
            shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_delete_shift(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00")
    assert shift.delete_shift(conn, 111) == 1
    assert shift.delete_shift(conn, 111) == 0


def test_describe_empty(tmp_path):
    conn = connect(tmp_path)
    assert "uncovered" in shift.describe(conn).lower()


def test_describe_lists_days(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111100")
    out = shift.describe(conn)
    assert "@111" in out
    assert "Mon" in out and "Sun" not in out


def test_shifts_mod_on_duty_within_window(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)  # a Monday
    assert shifts_mod.on_duty(conn, now) == [111]
    assert shifts_mod.is_covered(conn, now) is True


def test_shifts_mod_outside_window(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    now = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
    assert shifts_mod.on_duty(conn, now) == []
    assert shifts_mod.is_covered(conn, now) is False


def test_shifts_mod_cross_midnight(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "22:00", "02:00", "1111111")
    assert 111 in shifts_mod.on_duty(conn, datetime(2026, 1, 5, 23, 30, tzinfo=UTC))
    assert 111 in shifts_mod.on_duty(conn, datetime(2026, 1, 6, 1, 0, tzinfo=UTC))
    assert 111 not in shifts_mod.on_duty(conn, datetime(2026, 1, 6, 12, 0, tzinfo=UTC))


def test_shifts_mod_timezone_aware(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "Asia/Jakarta", "09:00", "17:00", "1111111")
    # 03:00 UTC = 10:00 WIB, inside the window
    assert 111 in shifts_mod.on_duty(conn, datetime(2026, 1, 5, 3, 0, tzinfo=UTC))
    # 12:00 UTC = 19:00 WIB, outside
    assert 111 not in shifts_mod.on_duty(conn, datetime(2026, 1, 5, 12, 0, tzinfo=UTC))


def test_shifts_mod_respects_days_mask(tmp_path):
    conn = connect(tmp_path)
    # Monday only
    shift.upsert_shift(conn, 111, "UTC", "00:00", "23:59", "1000000")
    assert 111 in shifts_mod.on_duty(conn, datetime(2026, 1, 5, 12, 0, tzinfo=UTC))  # Monday
    assert 111 not in shifts_mod.on_duty(conn, datetime(2026, 1, 6, 12, 0, tzinfo=UTC))  # Tuesday


def test_covered_minutes_per_day(tmp_path):
    conn = connect(tmp_path)
    assert shift.covered_minutes_per_day(conn) == 0
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    assert shift.covered_minutes_per_day(conn) == 8 * 60
    shift.upsert_shift(conn, 222, "UTC", "17:00", "23:00", "1111111")
    assert shift.covered_minutes_per_day(conn) == 14 * 60


def test_overlapping_shifts_do_not_double_count(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    shift.upsert_shift(conn, 222, "UTC", "09:00", "17:00", "1111111")
    assert shift.covered_minutes_per_day(conn) == 8 * 60, "identical windows union to one window"


def test_contiguous_shifts_union_to_full_day(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "00:00", "12:00", "1111111")
    shift.upsert_shift(conn, 222, "UTC", "12:00", "23:59", "1111111")
    assert shift.covered_minutes_per_day(conn) == 23 * 60 + 59


def test_cross_midnight_shifts_union(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "22:00", "02:00", "1111111")
    shift.upsert_shift(conn, 222, "UTC", "00:00", "23:00", "1111111")
    total = shift.covered_minutes_per_day(conn)
    assert total == 24 * 60, "the full day shift covers everything including the night window"


def test_next_boundary(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    nxt = shifts_mod.next_boundary(conn, now)
    assert nxt is not None
    assert nxt > now