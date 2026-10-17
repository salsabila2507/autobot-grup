from engine import db as dbm
from engine import identity as idm
from tests.helpers import connect, dt, iso


def _identity(conn, tg_id, username, handle):
    ident, _ = idm.bind_handle(conn, tg_id, username, handle)
    return ident


# --- quarantine ------------------------------------------------------

def test_quarantine_account_blocks_drops(tmp_path):
    conn = connect(tmp_path)
    now = dt(2026, 1, 1, 12)
    ident = _identity(conn, 111, "alice", "alice_x")
    idm.quarantine_account(conn, 111)
    res = idm.gate_drop(conn, "g", "eng_blue", 1, 111, "alice", "alice_x", "1", 30, 4, now)
    assert not res.ok
    assert res.reason == "quarantine"


def test_quarantine_is_reversible(tmp_path):
    conn = connect(tmp_path)
    now = dt(2026, 1, 1, 12)
    _identity(conn, 111, "alice", "alice_x")
    idm.quarantine_account(conn, 111)
    dbm.ex(conn, "UPDATE accounts SET status='active' WHERE tg_user_id=111")
    res = idm.gate_drop(conn, "g", "eng_blue", 1, 111, "alice", "alice_x", "1", 30, 4, now)
    assert res.ok


# --- alt cases -------------------------------------------------------

def test_open_alt_case_records_signals(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn, 111, "alice", "alice_x")
    alt_id = idm.open_alt_case(
        conn, ident.id, primary_account_id=None, alt_tg_user_id=222,
        signals=["hard:same_x_handle"], score=100,
    )
    row = dbm.q1(conn, "SELECT * FROM alt_cases WHERE id=?", (alt_id,))
    assert row["status"] == "open"
    assert row["score"] == 100
    assert "hard:same_x_handle" in row["signals"]
    assert row["identity_id"] == ident.id
    assert row["primary_account_id"] is not None


def test_open_alt_case_tracks_alt_account(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn, 111, "alice", "alice_x")
    dbm.ex(conn, "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)", (ident.id, 222, "bob"))
    alt_id = idm.open_alt_case(conn, ident.id, None, alt_tg_user_id=222, signals=["hard:same_x_handle"], score=100)
    row = dbm.q1(conn, "SELECT * FROM alt_cases WHERE id=?", (alt_id,))
    assert row["alt_account_id"] is not None


def test_open_alt_case_without_alt_account(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn, 111, "alice", "alice_x")
    alt_id = idm.open_alt_case(conn, ident.id, None, alt_tg_user_id=None, signals=[], score=0)
    row = dbm.q1(conn, "SELECT * FROM alt_cases WHERE id=?", (alt_id,))
    assert row["alt_account_id"] is None


# --- propagation -----------------------------------------------------

def test_propagate_sets_all_accounts(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn, 111, "alice", "alice_x")
    dbm.ex(conn, "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)", (ident.id, 222, "bob"))
    dbm.ex(conn, "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)", (ident.id, 333, "carol"))
    n = idm.propagate_to_identity(conn, ident.id, "banned")
    assert n == 3
    rows = dbm.q(conn, "SELECT status FROM accounts WHERE identity_id=?", (ident.id,))
    assert all(r["status"] == "banned" for r in rows)


def test_propagate_does_not_undo_banned(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn, 111, "alice", "alice_x")
    dbm.ex(conn, "INSERT INTO accounts (identity_id, tg_user_id, tg_username, status) VALUES (?,?,?, 'banned')", (ident.id, 222, "bob"))
    idm.propagate_to_identity(conn, ident.id, "muted")
    row = dbm.q1(conn, "SELECT status FROM accounts WHERE tg_user_id=222")
    assert row["status"] == "banned"


def test_blacklist_identity_blocks_fresh_account(tmp_path):
    conn = connect(tmp_path)
    now = dt(2026, 1, 1, 12)
    ident = _identity(conn, 111, "alice", "alice_x")
    idm.blacklist_identity(conn, ident.id)
    fresh = idm.gate_drop(conn, "g", "eng_blue", 1, 999, "stranger", "alice_x", "1", 30, 4, now)
    assert not fresh.ok
    assert fresh.reason == "banned"


# --- welcome flow ----------------------------------------------------

def test_bind_handle_is_idempotent(tmp_path):
    conn = connect(tmp_path)
    first, created_first = idm.bind_handle(conn, 111, "alice", "alice_x")
    second, created_second = idm.bind_handle(conn, 111, "alice", "alice_x")
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_bind_handle_rejects_conflict(tmp_path):
    conn = connect(tmp_path)
    idm.bind_handle(conn, 111, "alice", "alice_x")
    try:
        idm.bind_handle(conn, 222, "bob", "alice_x")
    except ValueError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_bind_handle_rejects_self_conflict(tmp_path):
    conn = connect(tmp_path)
    idm.bind_handle(conn, 111, "alice", "alice_x")
    try:
        idm.bind_handle(conn, 111, "alice", "different_x")
    except ValueError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected ValueError")