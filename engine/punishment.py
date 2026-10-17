from dataclasses import dataclass
from datetime import datetime, timedelta

from engine import db as dbm


@dataclass
class Action:
    kind: str
    level: int
    duration: str
    reason: str


def parse_duration(value: str) -> timedelta:
    v = value.strip().lower()
    if v.endswith("h"):
        return timedelta(hours=int(v[:-1]))
    if v.endswith("d"):
        return timedelta(days=int(v[:-1]))
    if v.endswith("m"):
        return timedelta(minutes=int(v[:-1]))
    raise ValueError(f"invalid duration: {value}")


def next_action(identity, case, rules):
    strikes = identity.strikes if identity is not None else 0
    if strikes >= rules.max_warnings:
        return None
    level = strikes + 1
    duration = rules.warn_ladder.get(level)
    if not duration:
        return None
    return Action(
        kind="warn",
        level=level,
        duration=duration,
        reason=f"case {case.id}: missed {case.missing_count} required engagements",
    )


def dispatch(conn, case, action, mode, actor_tg_user_id=None, dry_run=True, execute=None):
    now = datetime.now(dbm.timezone.utc)
    expires = (now + parse_duration(action.duration)).isoformat(timespec="seconds")
    intent = {
        "identity_id": case.identity_id,
        "case_id": case.id,
        "kind": action.kind,
        "level": action.level,
        "duration": action.duration,
        "expires_at": expires,
        "mode": mode,
        "actor_tg_user_id": actor_tg_user_id,
        "reason": action.reason,
    }
    if dry_run:
        intent["dry_run"] = True
        return intent
    stamp = dbm.now_iso()
    pid = dbm.ex(
        conn,
        "INSERT INTO punishments (identity_id, case_id, kind, level, expires_at, reason, actor_tg_user_id, mode, issued_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (case.identity_id, case.id, action.kind, action.level, expires, action.reason, actor_tg_user_id, mode, stamp),
    )
    dbm.ex(
        conn,
        "UPDATE cases SET status=?, closed_at=?, closed_by=?, close_reason=? WHERE id=? AND status=?",
        (dbm.CASE_ACTIONED, stamp, actor_tg_user_id,
         f"actioned:{action.kind}:{action.level}", case.id, dbm.CASE_OPEN),
    )
    if action.kind in ("mute", "ban"):
        from engine import identity as idm

        idm.propagate_to_identity(conn, case.identity_id, action.kind + "d" if action.kind == "mute" else "banned")
        if action.kind == "ban":
            idm.blacklist_identity(conn, case.identity_id)
    intent["punishment_id"] = pid
    if execute is not None:
        execute(intent)
    return intent


def warn_level_from_strikes(strikes: int, rules):
    level = strikes + 1
    if level > rules.max_warnings:
        return None
    duration = rules.warn_ladder.get(level)
    if not duration:
        return None
    return level, duration
