from datetime import datetime, time

from zoneinfo import ZoneInfo

from engine import db as dbm


def _shift_windows_utc(conn, now_utc: datetime):
    rows = dbm.q(conn, "SELECT * FROM shifts")
    for s in rows:
        tz = ZoneInfo(s["tz"])
        local = now_utc.astimezone(tz)
        start_h, start_m = (int(x) for x in s["start_local"].split(":"))
        end_h, end_m = (int(x) for x in s["end_local"].split(":"))
        day_idx = local.weekday()
        if s["days_mask"][day_idx] != "1":
            continue
        yield s, start_h, start_m, end_h, end_m, local


def on_duty(conn, now_utc: datetime) -> list[int]:
    ids = []
    for s, sh, sm, eh, em, local in _shift_windows_utc(conn, now_utc):
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        local_minutes = local.hour * 60 + local.minute
        if start_min < end_min:
            covered = start_min <= local_minutes < end_min
        else:
            covered = local_minutes >= start_min or local_minutes < end_min
        if covered and s["admin_tg_user_id"] not in ids:
            ids.append(s["admin_tg_user_id"])
    return ids


def is_covered(conn, now_utc: datetime) -> bool:
    return bool(on_duty(conn, now_utc))


def next_boundary(conn, now_utc: datetime) -> datetime | None:
    best = None
    for s, sh, sm, eh, em, local in _shift_windows_utc(conn, now_utc):
        tz = ZoneInfo(s["tz"])
        today = local.date()
        start_dt = datetime.combine(today, time(sh, sm), tzinfo=tz)
        end_dt = datetime.combine(today, time(eh, em), tzinfo=tz)
        for cand in (start_dt, end_dt):
            if cand.astimezone(ZoneInfo("UTC")) > now_utc:
                cand_utc = cand.astimezone(ZoneInfo("UTC"))
                if best is None or cand_utc < best:
                    best = cand_utc
    return best
