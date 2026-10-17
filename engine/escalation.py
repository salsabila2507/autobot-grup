from dataclasses import dataclass
from datetime import datetime, timedelta

from engine import db as dbm
from engine import engine as eng
from engine import identity as idm
from engine import outbox
from engine import punishment as pm


def cases_past_deadline(conn, group_key: str, rules, now: datetime):
    cutoff = (now - timedelta(minutes=rules.enforce_deadline_minutes)).isoformat(timespec="seconds")
    return dbm.q(
        conn,
        "SELECT c.* FROM cases c JOIN drops d ON d.id=c.drop_id "
        "WHERE c.status='open' AND d.group_key=? AND c.opened_at<=? ORDER BY c.id",
        (group_key, cutoff),
    )


def escalate_case(conn, group, rules, case_row, mode: str, now: datetime):
    identity = idm.identity_by_id(conn, case_row["identity_id"])
    if identity is None:
        return None
    case = eng.Case(
        id=case_row["id"],
        identity_id=case_row["identity_id"],
        drop_id=case_row["drop_id"],
        missing_count=case_row["missing_count"],
        status=case_row["status"],
    )
    action = pm.next_action(identity, case, rules)
    if action is None:
        return None

    def execute(intent):
        outbox.queue_punish(conn, case.identity_id, case.id, intent)

    result = pm.dispatch(conn, case, action, mode, actor_tg_user_id=None, dry_run=False, execute=execute)
    return result


def open_obligations_before(conn, identity_ids=None, before: str = None):
    if identity_ids:
        ph = ",".join("?" * len(identity_ids))
        return dbm.q(
            conn,
            f"SELECT * FROM obligations WHERE cleared_at IS NULL AND created_at<? AND identity_id IN ({ph})",
            tuple([before] + list(identity_ids)),
        )
    return dbm.q(conn, "SELECT * FROM obligations WHERE cleared_at IS NULL AND created_at<?", (before,))


def boundary_warning(conn, group_key: str, rules, obligation_row, boundary_opened_at: str, now: datetime):
    """One warning per open obligation per round boundary. Idempotent via kv guard."""
    identity = idm.identity_by_id(conn, obligation_row["identity_id"])
    if identity is None or identity.vip:
        return None
    # A case against this same drop is the stricter path and issues its own punishment.
    # Warning here as well would double-count strikes for one missed engagement, which
    # "every open obligation produces exactly one warning" forbids.
    chase = dbm.q1(
        conn,
        "SELECT id FROM cases WHERE drop_id=? AND status IN (?,?)",
        (obligation_row["drop_id"], dbm.CASE_OPEN, dbm.CASE_ACTIONED),
    )
    if chase:
        return None
    guard = f"boundary_warn:{group_key}:{identity.id}:{boundary_opened_at}"
    if dbm.q1(conn, "SELECT value FROM kv WHERE key=?", (guard,)):
        return None
    ladder = pm.warn_level_from_strikes(identity.strikes, rules)
    if ladder is None:
        return None
    level, duration = ladder
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (guard, dbm.now_iso()))
    expires = (now + pm.parse_duration(duration)).isoformat(timespec="seconds")
    reason = f"obligation_boundary:{boundary_opened_at}"
    pid = dbm.ex(
        conn,
        "INSERT INTO punishments (identity_id, case_id, kind, level, expires_at, reason, actor_tg_user_id, mode, issued_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (identity.id, None, "warn", level, expires, reason, None, "bot_auto", dbm.now_iso()),
    )
    outbox.queue_punish(
        conn,
        identity.id,
        None,
        {"kind": "warn", "level": level, "duration": duration, "reason": reason, "mode": "bot_auto"},
    )
    return {"punishment_id": pid, "identity_id": identity.id, "level": level, "duration": duration}
