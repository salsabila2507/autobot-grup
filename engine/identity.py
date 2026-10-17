from datetime import datetime, timedelta

from engine import db as dbm
from engine.models import Account, GateResult, Identity


def _identity_from(row, tg_username=None):
    if row is None:
        return None
    return Identity(
        id=row["id"],
        x_handle=row["x_handle"],
        x_user_id=row["x_user_id"],
        vip=bool(row["vip"]),
        status=row["status"],
        strikes=row["strikes"],
        blacklisted_at=row["blacklisted_at"],
        first_seen_at=row["first_seen_at"],
        tg_username=tg_username,
    )


def _account_from(row):
    if row is None:
        return None
    return Account(
        id=row["id"],
        identity_id=row["identity_id"],
        tg_user_id=row["tg_user_id"],
        tg_username=row["tg_username"],
        joined_at=row["joined_at"],
        invite_link=row["invite_link"],
        status=row["status"],
    )


def clean_handle(handle: str) -> str:
    return (handle or "").strip().lstrip("@").lower()


def resolve_identity(conn, tg_user_id: int):
    row = dbm.q1(
        conn,
        "SELECT i.*, a.tg_username FROM identities i JOIN accounts a ON a.identity_id=i.id WHERE a.tg_user_id=?",
        (tg_user_id,),
    )
    return _identity_from(row, tg_username=row["tg_username"] if row else None)


def resolve_account(conn, tg_user_id: int):
    row = dbm.q1(conn, "SELECT * FROM accounts WHERE tg_user_id=?", (tg_user_id,))
    return _account_from(row)


def identity_by_handle(conn, handle: str):
    row = dbm.q1(conn, "SELECT * FROM identities WHERE x_handle=?", (clean_handle(handle),))
    return _identity_from(row)


def identity_by_id(conn, identity_id: int):
    row = dbm.q1(
        conn,
        "SELECT i.*, a.tg_username FROM identities i "
        "LEFT JOIN accounts a ON a.identity_id=i.id WHERE i.id=? LIMIT 1",
        (identity_id,),
    )
    return _identity_from(row, tg_username=row["tg_username"] if row else None)


def bind_handle(conn, tg_user_id: int, tg_username, handle: str):
    handle = clean_handle(handle)
    if not handle:
        raise ValueError("empty handle")
    stamp = dbm.now_iso()
    existing = resolve_identity(conn, tg_user_id)
    if existing:
        if existing.x_handle == handle:
            return existing, False
        raise ValueError(f"tg account {tg_user_id} already bound to {existing.x_handle}")
    if identity_by_handle(conn, handle) is not None:
        raise ValueError(f"handle {handle} already bound to another account")
    iid = dbm.ex(conn, "INSERT INTO identities (x_handle, first_seen_at) VALUES (?,?)", (handle, stamp))
    dbm.ex(
        conn,
        "INSERT INTO accounts (identity_id, tg_user_id, tg_username, joined_at, status) VALUES (?,?,?,?,?)",
        (iid, tg_user_id, tg_username, stamp, "active"),
    )
    return identity_by_id(conn, iid), True


def _insert_alt_case(conn, identity_id: int, primary_account_id):
    dbm.ex(
        conn,
        "INSERT INTO alt_cases (identity_id, primary_account_id, alt_account_id, score, signals, status) VALUES (?,?,?,?,?,?)",
        (identity_id, primary_account_id, None, 100, '["hard:same_x_handle"]', "open"),
    )


def rebind_handle(conn, tg_user_id: int, new_handle: str):
    """Admin only handle change. Members never rebind themselves.

    A member who wants a different X handle has to ask an admin to run
    /bind <tg_user_id> <username_x>. The handle is the identity anchor, so changing
    it is an audited admin action, not a self service.
    """
    new_handle = clean_handle(new_handle)
    if not new_handle:
        raise ValueError("empty handle")
    identity = resolve_identity(conn, tg_user_id)
    if identity is None:
        raise LookupError("tg account has no identity yet, let them drop a link first")
    if identity.x_handle == new_handle:
        return identity
    holder = identity_by_handle(conn, new_handle)
    if holder is not None and holder.id != identity.id:
        raise ValueError(f"handle {new_handle} is already bound to another account")
    old = identity.x_handle
    dbm.ex(conn, "UPDATE identities SET x_handle=? WHERE id=?", (new_handle, identity.id))
    dbm.ex(
        conn,
        "INSERT INTO external_actions (source_bot, raw_text, status, target_tg_user_id, kind, level, duration, observed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("queue_bot", f"handle rebind {old} -> {new_handle}", "parsed", None, "rebind", None, None, dbm.now_iso()),
    )
    return identity_by_id(conn, identity.id)


def _account_id_of(conn, identity_id: int):
    row = dbm.q1(
        conn,
        "SELECT id FROM accounts WHERE identity_id=? ORDER BY id LIMIT 1",
        (identity_id,),
    )
    return row["id"] if row else None


def open_alt_case(conn, identity_id, primary_account_id, alt_tg_user_id=None, signals=None, score: int = 0) -> int:
    """Alt case from a welcome flow submission. Returns the alt_cases id."""
    alt_account_id = None
    if alt_tg_user_id:
        row = dbm.q1(conn, "SELECT id FROM accounts WHERE tg_user_id=?", (alt_tg_user_id,))
        alt_account_id = row["id"] if row else None
    import json as _json

    return dbm.ex(
        conn,
        "INSERT INTO alt_cases (identity_id, primary_account_id, alt_account_id, score, signals, status) "
        "VALUES (?,?,?,?,?, 'open')",
        (
            identity_id,
            primary_account_id if primary_account_id is not None else _account_id_of(conn, identity_id),
            alt_account_id,
            score,
            _json.dumps(signals or []),
        ),
    )


def quarantine_account(conn, tg_user_id: int):
    """Can read, cannot drop. Held until an admin decides."""
    return dbm.ex(conn, "UPDATE accounts SET status='quarantined' WHERE tg_user_id=?", (tg_user_id,))


def propagate_to_identity(conn, identity_id: int, new_status: str):
    """Warn, mute and ban apply to every known account of the identity."""
    return dbm.ex(
        conn,
        "UPDATE accounts SET status=? WHERE identity_id=? AND status!='banned'",
        (new_status, identity_id),
    )


def blacklist_identity(conn, identity_id: int):
    """Ban also blacklists the X handle: banned handle never enters the queue again."""
    dbm.ex(conn, "UPDATE identities SET status='banned', blacklisted_at=? WHERE id=?", (dbm.now_iso(), identity_id))


def sync_group_admins(conn, group_key: str, admin_user_ids) -> int:
    """Mirror config admin ids into the DB so gate_drop can check admin status cheaply."""
    n = 0
    for admin_id in admin_user_ids or []:
        dbm.ex(
            conn,
            "INSERT OR REPLACE INTO group_admins (group_key, tg_user_id, added_at) VALUES (?,?,?)",
            (group_key, int(admin_id), dbm.now_iso()),
        )
        n += 1
    return n


def is_admin_of(conn, group_key: str, tg_user_id) -> bool:
    """Admins are never limited: no obligation gate, no VIP cap, no duplicate block.

    Safety gates (banned, quarantined, handle already taken) still apply, those are
    identity guards, not drop limits.
    """
    if not tg_user_id:
        return False
    row = dbm.q1(
        conn,
        "SELECT tg_user_id FROM group_admins WHERE group_key=? AND tg_user_id=?",
        (group_key, int(tg_user_id)),
    )
    return row is not None


def gate_drop(conn, group_key, circle_key, round_id, tg_user_id, tg_username, handle, tweet_id, required, vip_limit, now: datetime, is_admin: bool = False) -> GateResult:
    handle = clean_handle(handle)
    stamp = now.isoformat(timespec="seconds")
    account = resolve_account(conn, tg_user_id)
    identity = resolve_identity(conn, tg_user_id)
    if account and account.status == "banned":
        return GateResult(False, "banned", identity, account)
    if account and account.status == "quarantined":
        return GateResult(False, "quarantine", identity, account)
    if identity and (identity.status == "banned" or identity.blacklisted_at):
        return GateResult(False, "banned", identity, account)
    if identity is None:
        taken = dbm.q1(
            conn,
            "SELECT i.*, a.id AS account_id, a.tg_username FROM identities i "
            "JOIN accounts a ON a.identity_id=i.id WHERE i.x_handle=?",
            (handle,),
        )
        if taken:
            _insert_alt_case(conn, taken["id"], taken["account_id"])
            taken_identity = _identity_from(taken, tg_username=taken["tg_username"])
            if taken_identity.status == "banned" or taken_identity.blacklisted_at:
                return GateResult(False, "banned", taken_identity, account)
            return GateResult(False, "handle_taken", taken_identity, account)
        iid = dbm.ex(conn, "INSERT INTO identities (x_handle, first_seen_at) VALUES (?,?)", (handle, stamp))
        aid = dbm.ex(
            conn,
            "INSERT INTO accounts (identity_id, tg_user_id, tg_username, joined_at, status) VALUES (?,?,?,?,?)",
            (iid, tg_user_id, tg_username, stamp, "active"),
        )
        identity = identity_by_id(conn, iid)
        account = _account_from(dbm.q1(conn, "SELECT * FROM accounts WHERE id=?", (aid,)))
    elif account is None:
        aid = dbm.ex(
            conn,
            "INSERT INTO accounts (identity_id, tg_user_id, tg_username, joined_at, status) VALUES (?,?,?,?,?)",
            (identity.id, tg_user_id, tg_username, stamp, "active"),
        )
        account = _account_from(dbm.q1(conn, "SELECT * FROM accounts WHERE id=?", (aid,)))
    if is_admin:
        return GateResult(True, None, identity, account)
    credits = unconsumed_credits(conn, identity.id)
    if identity.vip:
        window_start = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        n = dbm.q1(conn, "SELECT COUNT(*) AS n FROM drops WHERE identity_id=? AND created_at>=?", (identity.id, window_start))["n"]
        if n >= vip_limit and credits == 0:
            return GateResult(False, "vip_limit", identity, account)
    else:
        if credits > 0:
            # member re-dropping after a wrong link deletion: skip the obligation gate once
            return GateResult(True, None, identity, account)
        open_ob = dbm.q1(conn, "SELECT id FROM obligations WHERE identity_id=? AND cleared_at IS NULL", (identity.id,))
        if open_ob:
            return GateResult(False, "obligation_open", identity, account)
    if tweet_id:
        dup = dbm.q1(conn, "SELECT id FROM drops WHERE round_id=? AND identity_id=? AND tweet_id=?", (round_id, identity.id, tweet_id))
        if dup:
            return GateResult(False, "duplicate", identity, account)
    return GateResult(True, None, identity, account)


def create_obligation(conn, identity_id: int, drop_id: int, required: int) -> int:
    return dbm.ex(
        conn,
        "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
        (identity_id, drop_id, required, dbm.now_iso()),
    )


def clear_obligations(conn, identity_id: int) -> int:
    rows = dbm.q(conn, "SELECT id FROM obligations WHERE identity_id=? AND cleared_at IS NULL", (identity_id,))
    stamp = dbm.now_iso()
    for r in rows:
        dbm.ex(conn, "UPDATE obligations SET cleared_at=? WHERE id=?", (stamp, r["id"]))
    return len(rows)


def close_open_cases(conn, identity_id: int, closed_by=None, reason: str = "admin_reset") -> int:
    """Close every open case for an identity.

    Clearing the debt without closing the case left the punishment pipeline free to act
    on a verdict the admin had just overturned, so an admin reset has to close both.
    """
    rows = dbm.q(
        conn,
        "SELECT id FROM cases WHERE identity_id=? AND status IN (?,?)",
        (identity_id, dbm.CASE_OPEN, dbm.CASE_ACTIONED),
    )
    stamp = dbm.now_iso()
    for r in rows:
        dbm.ex(
            conn,
            "UPDATE cases SET status=?, closed_at=?, closed_by=?, close_reason=? WHERE id=?",
            (dbm.CASE_CLEARED, stamp, closed_by, reason, r["id"]),
        )
    return len(rows)


def reset_member(conn, identity_id: int, closed_by=None) -> dict:
    """Put one member back to normal: no debt, no open case, nothing queued to punish."""
    obligations = clear_obligations(conn, identity_id)
    cases = close_open_cases(conn, identity_id, closed_by=closed_by)
    # rowcount, not lastrowid: dbm.ex returns the last inserted rowid, so using it
    # here reported "cancelled 6147 punishment(s)" when the real number was 2.
    # The status is 'queued', which is what the punishment worker actually reads. The
    # old filter said 'pending' and matched nothing, so /cleared never cancelled
    # anything that was really waiting to be sent.
    cancelled = conn.execute(
        "UPDATE punish_intents SET status='cancelled', last_error='admin_reset' "
        "WHERE identity_id=? AND status='queued'",
        (identity_id,),
    ).rowcount
    conn.commit()
    return {"obligations": obligations, "cases": cases, "punish_intents_cancelled": cancelled}


def set_vip(conn, tg_user_id: int, vip: bool) -> bool:
    identity = resolve_identity(conn, tg_user_id)
    if identity is None:
        return False
    dbm.ex(conn, "UPDATE identities SET vip=? WHERE id=?", (1 if vip else 0, identity.id))
    return True


def identity_by_tg_username(conn, username: str):
    row = dbm.q1(
        conn,
        "SELECT i.*, a.tg_username FROM identities i JOIN accounts a ON a.identity_id=i.id "
        "WHERE LOWER(a.tg_username)=LOWER(?) LIMIT 1",
        (str(username or "").lstrip("@"),),
    )
    return _identity_from(row, tg_username=row["tg_username"] if row else None)


def set_vip_by_username(conn, username: str, vip: bool):
    """Admin-only VIP grant by Telegram username. Returns the identity, or None if unknown."""
    identity = identity_by_tg_username(conn, username)
    if identity is None:
        return None
    dbm.ex(conn, "UPDATE identities SET vip=? WHERE id=?", (1 if vip else 0, identity.id))
    return identity_by_id(conn, identity.id)


def grant_drop_credit(conn, identity_id: int, round_id, drop_id, reason: str) -> int:
    """A wrong link deletion never costs the member their slot. Credits do not count
    against the VIP cap or per round accounting."""
    return dbm.ex(
        conn,
        "INSERT INTO drop_credits (identity_id, round_id, drop_id, granted_for, reason, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (identity_id, round_id, drop_id, str(drop_id), reason or "wrong_link", dbm.now_iso()),
    )


def unconsumed_credits(conn, identity_id: int) -> int:
    row = dbm.q1(
        conn,
        "SELECT COUNT(*) AS n FROM drop_credits WHERE identity_id=? AND consumed_at IS NULL",
        (identity_id,),
    )
    return row["n"]


def consume_drop_credit(conn, identity_id: int) -> int | None:
    row = dbm.q1(
        conn,
        "SELECT id FROM drop_credits WHERE identity_id=? AND consumed_at IS NULL ORDER BY id LIMIT 1",
        (identity_id,),
    )
    if row is None:
        return None
    dbm.ex(conn, "UPDATE drop_credits SET consumed_at=? WHERE id=?", (dbm.now_iso(), row["id"]))
    return row["id"]


def record_early_drop(conn, identity_id: int, group_key: str, now: datetime):
    window_start = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    n = dbm.q1(
        conn,
        "SELECT COUNT(*) AS n FROM early_drop_attempts WHERE identity_id=? AND attempted_at>=?",
        (identity_id, window_start),
    )["n"]
    warned = False
    if n >= 1:
        already = dbm.q1(
            conn,
            "SELECT id FROM punishments WHERE identity_id=? AND reason='early_drop' AND issued_at>=?",
            (identity_id, window_start),
        )
        if not already:
            expires = (now + timedelta(hours=4)).isoformat(timespec="seconds")
            dbm.ex(
                conn,
                "INSERT INTO punishments (identity_id, kind, level, expires_at, reason, actor_tg_user_id, mode, issued_at) VALUES (?,?,?,?,?,?,?,?)",
                (identity_id, "warn", 1, expires, "early_drop", None, "bot_auto", dbm.now_iso()),
            )
            warned = True
    dbm.ex(
        conn,
        "INSERT INTO early_drop_attempts (identity_id, group_key, attempted_at) VALUES (?,?,?)",
        (identity_id, group_key, dbm.now_iso()),
    )
    return n + 1, warned
