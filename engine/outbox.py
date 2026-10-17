import json

from engine import db as dbm


def enqueue(conn, group_key: str, kind: str, payload: dict) -> int:
    oid = dbm.ex(
        conn,
        "INSERT INTO outbox (group_key, kind, payload, status, created_at) VALUES (?,?,?,'queued',?)",
        (group_key, kind, json.dumps(payload), dbm.now_iso()),
    )
    return oid


def pending(conn, limit: int = 10):
    return dbm.q(
        conn,
        "SELECT * FROM outbox WHERE status='queued' AND attempts < 5 ORDER BY id LIMIT ?",
        (limit,),
    )


def payload_of(row) -> dict:
    return json.loads(row["payload"])


def mark_sent(conn, outbox_id: int):
    dbm.ex(
        conn,
        "UPDATE outbox SET status='sent', sent_at=? WHERE id=?",
        (dbm.now_iso(), outbox_id),
    )




def bump_attempt(conn, outbox_id: int, error: str):
    dbm.ex(
        conn,
        "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
        (error, outbox_id),
    )


def queue_punish(conn, identity_id: int, case_id, intent: dict) -> int:
    return dbm.ex(
        conn,
        "INSERT INTO punish_intents (identity_id, case_id, kind, level, duration, reason, mode, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,'queued',?)",
        (
            identity_id,
            case_id,
            intent.get("kind"),
            intent.get("level"),
            intent.get("duration"),
            intent.get("reason"),
            intent.get("mode"),
            dbm.now_iso(),
        ),
    )


def pending_punish(conn, limit: int = 5):
    return dbm.q(
        conn,
        "SELECT * FROM punish_intents WHERE status='queued' AND attempts < 5 ORDER BY id LIMIT ?",
        (limit,),
    )


def mark_punish_sent(conn, intent_id: int):
    dbm.ex(
        conn,
        "UPDATE punish_intents SET status='sent', sent_at=? WHERE id=?",
        (dbm.now_iso(), intent_id),
    )


def mark_punish_failed(conn, intent_id: int, error: str):
    dbm.ex(
        conn,
        "UPDATE punish_intents SET status='failed', last_error=? WHERE id=?",
        (error, intent_id),
    )


def bump_punish_attempt(conn, intent_id: int, error: str):
    dbm.ex(
        conn,
        "UPDATE punish_intents SET attempts=attempts+1, last_error=? WHERE id=?",
        (error, intent_id),
    )
