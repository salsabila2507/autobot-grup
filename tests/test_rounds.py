from engine import rounds
from engine import db as dbm
from tests.helpers import connect, dt

SCHEDULE = ["06:00-18:00", "18:00-06:00"]


def test_parse_schedule_ok():
    windows = rounds.parse_schedule(SCHEDULE)
    assert windows == [(360, 1080), (1080, 360)]


def test_parse_schedule_must_cover_24h():
    try:
        rounds.parse_schedule(["06:00-18:00"])
    except ValueError as exc:
        assert "24h" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_schedule_rejects_overlap():
    try:
        rounds.parse_schedule(["06:00-18:00", "12:00-06:00"])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_parse_window_rejects_garbage():
    try:
        rounds.parse_window("noon-5pm")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_current_window_day():
    idx, start, end = rounds.current_window(SCHEDULE, dt(2026, 1, 1, 9, 30))
    assert idx == 0
    assert start == 360
    assert end == 1080


def test_current_window_night():
    idx, start, end = rounds.current_window(SCHEDULE, dt(2026, 1, 1, 23, 5))
    assert idx == 1
    assert start == 1080


def test_current_window_just_after_boundary():
    idx, _, _ = rounds.current_window(SCHEDULE, dt(2026, 1, 1, 18, 0))
    assert idx == 1


def test_current_window_last_minute_of_day():
    idx, _, _ = rounds.current_window(SCHEDULE, dt(2026, 1, 1, 23, 59))
    assert idx == 1


def test_window_bounds_day():
    _, start, end = rounds.window_bounds(SCHEDULE, dt(2026, 1, 1, 9, 30))
    assert start == dt(2026, 1, 1, 6, 0)
    assert end == dt(2026, 1, 1, 18, 0)


def test_window_bounds_cross_midnight():
    _, start, end = rounds.window_bounds(SCHEDULE, dt(2026, 1, 1, 23, 30))
    assert start == dt(2026, 1, 1, 18, 0)
    assert end == dt(2026, 1, 2, 6, 0)


def test_window_bounds_just_after_midnight():
    _, start, end = rounds.window_bounds(SCHEDULE, dt(2026, 1, 2, 0, 30))
    assert start == dt(2026, 1, 1, 18, 0)
    assert end == dt(2026, 1, 2, 6, 0)


def test_current_round_created_once(tmp_path):
    conn = connect(tmp_path)
    r1 = rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 0))
    r2 = rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 30))
    assert r1.id == r2.id
    assert r1.seq == 1
    assert r1.opened_at == dt(2026, 1, 1, 6, 0).isoformat(timespec="seconds")


def test_round_seq_increments_next_window(tmp_path):
    conn = connect(tmp_path)
    r1 = rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 0))
    r2 = rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 19, 0))
    assert r2.id != r1.id
    assert r2.seq == 2


def test_rounds_isolated_per_circle(tmp_path):
    conn = connect(tmp_path)
    a = rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 0))
    b = rounds.current_round(conn, "like20", SCHEDULE, dt(2026, 1, 1, 9, 0))
    assert a.id != b.id
    assert a.seq == b.seq == 1


def test_close_previous_rounds(tmp_path):
    conn = connect(tmp_path)
    rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 0))
    closed = rounds.close_previous_rounds(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 19, 0))
    assert closed == 1
    row = dbm.q1(conn, "SELECT closed_at FROM rounds WHERE seq=1")
    assert row["closed_at"] is not None


def test_close_previous_leaves_current_open(tmp_path):
    conn = connect(tmp_path)
    rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 9, 0))
    rounds.current_round(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 19, 0))
    rounds.close_previous_rounds(conn, "eng_blue", SCHEDULE, dt(2026, 1, 1, 19, 0))
    row = dbm.q1(conn, "SELECT closed_at FROM rounds WHERE seq=2")
    assert row["closed_at"] is None


def test_boundary_between_true_across_window():
    prev = dt(2026, 1, 1, 17, 59)
    now = dt(2026, 1, 1, 18, 1)
    assert rounds.boundary_between(SCHEDULE, prev, now) is True


def test_boundary_between_false_within_window():
    prev = dt(2026, 1, 1, 9, 0)
    now = dt(2026, 1, 1, 9, 1)
    assert rounds.boundary_between(SCHEDULE, prev, now) is False


def test_boundary_between_true_across_days():
    prev = dt(2026, 1, 1, 7, 0)
    now = dt(2026, 1, 2, 7, 0)
    assert rounds.boundary_between(SCHEDULE, prev, now) is True
