from datetime import datetime, timedelta, timezone

from engine import db as dbm
from engine import identity as idm
from engine.models import Identity
from tests.helpers import connect, circle, rules, dt

UTC = timezone.utc


def _drop(conn, tg_user, handle, round_id=1, tweet_id="100", group="g", key="eng_blue", now=None):
    now = now or dt(2026, 1, 1, 12)
    result = idm.gate_drop(conn, group, key, round_id, tg_user, f"user{tg_user}", handle, tweet_id, 30, 4, now)
    if result.ok:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM drops").fetchone()["p"]
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group, round_id, key, pos, result.identity.id, result.account.id, "l", tweet_id, now.isoformat(timespec="seconds")),
        )
    return result


def test_second_account_same_handle_rejected(tmp_path):
    conn = connect(tmp_path)
    first = _drop(conn, 111, "shared")
    assert first.ok

    second = _drop(conn, 222, "shared", round_id=2)
    assert not second.ok
    assert second.reason == "handle_taken"

    alt = dbm.q1(conn, "SELECT * FROM alt_cases")
    assert alt is not None
    assert alt["score"] == 100
    assert "hard:same_x_handle" in alt["signals"]
    assert alt["status"] == "open"

    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM drops")["n"] == 1


def test_muted_identity_blocks_new_account(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "muted_one").ok
    dbm.ex(conn, "UPDATE identities SET status='muted' WHERE x_handle='muted_one'")

    second = _drop(conn, 333, "muted_one", round_id=2)
    assert not second.ok
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM drops")["n"] == 1


def test_banned_identity_blocks_fresh_account(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "banned_one").ok
    ident = idm.identity_by_handle(conn, "banned_one")
    dbm.ex(conn, "UPDATE identities SET blacklisted_at=? WHERE id=?", (dbm.now_iso(), ident.id))

    fresh = _drop(conn, 444, "banned_one", round_id=2)
    assert not fresh.ok
    assert fresh.reason == "banned"
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM drops")["n"] == 1


def test_quarantined_account_rejected(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "quar_one").ok
    acct = idm.resolve_account(conn, 111)
    dbm.ex(conn, "UPDATE accounts SET status='quarantined' WHERE id=?", (acct.id,))

    again = _drop(conn, 111, "quar_one", round_id=2)
    assert not again.ok
    assert again.reason == "quarantine"


def test_vip_exempt_from_obligation(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "vip_one").ok
    idm.set_vip(conn, 111, True)
    dbm.ex(
        conn,
        "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
        (1, 1, 30, dbm.now_iso()),
    )

    again = _drop(conn, 111, "vip_one", round_id=2, tweet_id="101")
    assert again.ok, "VIP skips the obligation gate"


def test_regular_member_blocked_by_open_obligation(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "reg_one").ok
    dbm.ex(
        conn,
        "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
        (1, 1, 30, dbm.now_iso()),
    )

    again = _drop(conn, 111, "reg_one", round_id=2, tweet_id="101")
    assert not again.ok
    assert again.reason == "obligation_open"


def test_clear_obligation_unblocks(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "reg_two").ok
    idm.create_obligation(conn, 1, 1, 30)
    assert not _drop(conn, 111, "reg_two", round_id=2, tweet_id="101").ok

    assert idm.clear_obligations(conn, 1) == 1
    assert _drop(conn, 111, "reg_two", round_id=2, tweet_id="101").ok


def test_duplicate_tweet_same_round_rejected(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "dup_one").ok
    again = _drop(conn, 111, "dup_one", round_id=1, tweet_id="100")
    assert not again.ok
    assert again.reason == "duplicate"


def test_vip_cap_rolling_24h(tmp_path):
    conn = connect(tmp_path)
    assert _drop(conn, 111, "vip_cap").ok
    idm.set_vip(conn, 111, True)
    ident = idm.resolve_identity(conn, 111)
    base = dt(2026, 1, 1, 12)
    for i in range(4):
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("g", i, "eng_blue", i + 1, ident.id, 1, "l", str(100 + i), (base - timedelta(hours=1)).isoformat(timespec="seconds")),
        )

    over = _drop(conn, 111, "vip_cap", round_id=9, tweet_id="999", now=base)
    assert not over.ok
    assert over.reason == "vip_limit"


def test_bind_handle_conflict_raises(tmp_path):
    conn = connect(tmp_path)
    idm.bind_handle(conn, 111, "alice", "alice_x")
    try:
        idm.bind_handle(conn, 222, "bob", "alice_x")
    except ValueError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_bind_handle_same_user_idempotent(tmp_path):
    conn = connect(tmp_path)
    _, created = idm.bind_handle(conn, 111, "alice", "alice_x")
    assert created
    _, created = idm.bind_handle(conn, 111, "alice", "alice_x")
    assert not created


def test_resolve_identity_carries_tg_username(tmp_path):
    conn = connect(tmp_path)
    idm.bind_handle(conn, 111, "alice", "alice_x")
    ident = idm.resolve_identity(conn, 111)
    assert ident.tg_username == "alice"
    assert ident.x_handle == "alice_x"
