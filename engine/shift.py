"""Admin shift registry: who covers moderation when, in their own timezone.

Shifts are used to decide whether an hour is "covered" so the engine knows when to
escalate automatically instead of waiting on a human.
"""

from engine import db as dbm

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def validate_tz(tz: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {tz}") from exc
    return tz


def validate_hhmm(value: str) -> str:
    parts = str(value or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {value}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time out of range: {value}")
    return f"{h:02d}:{m:02d}"


def upsert_shift(conn, admin_tg_user_id: int, tz: str, start_local: str, end_local: str, days_mask: str = "1111111") -> int:
    tz = validate_tz(tz)
    start_local = validate_hhmm(start_local)
    end_local = validate_hhmm(end_local)
    if start_local == end_local:
        raise ValueError("start and end cannot be identical")
    mask = str(days_mask or "1111111")
    if len(mask) != 7 or any(c not in "01" for c in mask):
        raise ValueError("days_mask must be 7 chars of 0 or 1, Mon..Sun")
    existing = dbm.q1(conn, "SELECT id FROM shifts WHERE admin_tg_user_id=?", (admin_tg_user_id,))
    if existing:
        dbm.ex(
            conn,
            "UPDATE shifts SET tz=?, start_local=?, end_local=?, days_mask=? WHERE id=?",
            (tz, start_local, end_local, mask, existing["id"]),
        )
        return existing["id"]
    return dbm.ex(
        conn,
        "INSERT INTO shifts (admin_tg_user_id, tz, start_local, end_local, days_mask) VALUES (?,?,?,?,?)",
        (admin_tg_user_id, tz, start_local, end_local, mask),
    )


def delete_shift(conn, admin_tg_user_id: int) -> int:
    rows = dbm.q(conn, "SELECT id FROM shifts WHERE admin_tg_user_id=?", (admin_tg_user_id,))
    for r in rows:
        dbm.ex(conn, "DELETE FROM shifts WHERE id=?", (r["id"],))
    return len(rows)


def describe(conn) -> str:
    rows = dbm.q(conn, "SELECT * FROM shifts ORDER BY start_local")
    if not rows:
        return "No shifts loaded. Every hour counts as uncovered, the ladder escalates on its own."
    lines = []
    for r in rows:
        days = "".join(d for d, on in zip(DAYS, r["days_mask"]) if on == "1") or "-"
        lines.append(
            f"@{r['admin_tg_user_id']} {r['start_local']}-{r['end_local']} {r['tz']} [{days}]"
        )
    return "\n".join(lines)


def _merge_intervals(intervals):
    if not intervals:
        return 0
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(e - s for s, e in merged)


def covered_minutes_per_day(conn) -> int:
    """Union of all shift windows across a day, in minutes.

    Overlapping shifts do not double count: two admins both covering 09:00-17:00
    covers 8 hours, not 16.
    """
    rows = dbm.q(conn, "SELECT * FROM shifts")
    if not rows:
        return 0
    per_day = {}
    for r in rows:
        mask = r["days_mask"]
        sh, sm = (int(x) for x in r["start_local"].split(":"))
        eh, em = (int(x) for x in r["end_local"].split(":"))
        start = sh * 60 + sm
        end = eh * 60 + em
        for idx, on in enumerate(mask):
            if on != "1":
                continue
            if start < end:
                ivs = per_day.setdefault(idx, [])
                ivs.append((start, end))
            else:
                ivs = per_day.setdefault(idx, [])
                ivs.append((start, 1440))
                ivs.append((0, end))
    if not per_day:
        return 0
    return min(1440, max(_merge_intervals(per_day[d]) for d in per_day))