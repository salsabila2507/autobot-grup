import re
from datetime import datetime, timedelta

from engine import db as dbm
from engine.models import Round

_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def parse_window(value: str):
    m = _WINDOW_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid window: {value}")
    sh, sm, eh, em = (int(x) for x in m.groups())
    start = sh * 60 + sm
    end = eh * 60 + em
    if not (0 <= start < 1440 and 0 <= end < 1440 and start != end):
        raise ValueError(f"invalid window: {value}")
    return start, end


def parse_schedule(schedule):
    if not schedule:
        raise ValueError("rounds.schedule_utc empty")
    windows = [parse_window(s) for s in schedule]
    total = 0
    for start, end in windows:
        total += (end - start) % 1440
    if total != 1440:
        raise ValueError("rounds.schedule_utc must cover 24h without overlap")
    return windows


def _windows(schedule):
    if schedule and isinstance(schedule[0], str):
        return parse_schedule(schedule)
    return list(schedule)


def current_window(schedule, now: datetime):
    minute = now.hour * 60 + now.minute
    for idx, (start, end) in enumerate(_windows(schedule)):
        if start < end and start <= minute < end:
            return idx, start, end
        if start > end and (minute >= start or minute < end):
            return idx, start, end
    raise RuntimeError("schedule validation bug")


def window_bounds(schedule, now: datetime):
    idx, start, end = current_window(schedule, now)
    start_dt = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
    if start_dt > now:
        start_dt -= timedelta(days=1)
    end_dt = start_dt + timedelta(minutes=(end - start) % 1440)
    return idx, start_dt, end_dt


def round_opened_at(schedule, now: datetime) -> str:
    _, start_dt, _ = window_bounds(schedule, now)
    return start_dt.isoformat(timespec="seconds")


def _round_from(row) -> Round:
    return Round(
        id=row["id"],
        circle_key=row["circle_key"],
        seq=row["seq"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
    )


def current_round(conn, circle_key: str, schedule, now: datetime) -> Round:
    opened_at = round_opened_at(schedule, now)
    row = dbm.q1(conn, "SELECT * FROM rounds WHERE circle_key=? AND opened_at=?", (circle_key, opened_at))
    if row:
        return _round_from(row)
    seq = dbm.q1(conn, "SELECT COUNT(*) AS n FROM rounds WHERE circle_key=?", (circle_key,))["n"] + 1
    rid = dbm.ex(conn, "INSERT INTO rounds (circle_key, seq, opened_at) VALUES (?,?,?)", (circle_key, seq, opened_at))
    return Round(id=rid, circle_key=circle_key, seq=seq, opened_at=opened_at)


def close_previous_rounds(conn, circle_key: str, schedule, now: datetime) -> int:
    opened_at = round_opened_at(schedule, now)
    rows = dbm.q(conn, "SELECT id FROM rounds WHERE circle_key=? AND opened_at<? AND closed_at IS NULL", (circle_key, opened_at))
    stamp = dbm.now_iso()
    for r in rows:
        dbm.ex(conn, "UPDATE rounds SET closed_at=? WHERE id=?", (stamp, r["id"]))
    return len(rows)


def boundary_between(schedule, prev: datetime, now: datetime) -> bool:
    _, prev_start, _ = window_bounds(schedule, prev)
    _, now_start, _ = window_bounds(schedule, now)
    return prev_start != now_start
