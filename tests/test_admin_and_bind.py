from engine import config as cfgmod
from engine import db as dbm
from engine import identity as idm
from engine import numbering
from engine import rounds as rounds_mod
from tests.helpers import connect, dt, iso, rules, write_config
from services import queue_bot as qb

SCHEDULE = ["06:00-18:00", "18:00-06:00"]


def _cfg(tmp_path, **over):
    return cfgmod.load(write_config(str(tmp_path / "c.yaml"), **over))


def _drop(conn, group_key, circle, tg_id, username, handle, tweet_id, round_id, now, vip_limit=4):
    return idm.gate_drop(
        conn, group_key, circle.key, round_id, tg_id, username, handle, tweet_id,
        circle.required, vip_limit, now,
    )


def _record(conn, group_key, circle, result, handle, tweet_id, now):
    position = numbering.next_position(conn, circle.key)
    return dbm.ex(
        conn,
        "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (group_key, result and 1, circle.key, position, result.identity.id, result.account.id,
         f"https://x.com/{handle}/status/{tweet_id}", tweet_id, iso(now)),
    )


# --- admin exemption -------------------------------------------------

def test_admin_skips_obligation_gate(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r1 = _drop(conn, g.key, c, 5, "adminuser", "admin_x", "1", 1, now)
    assert r1.ok
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
           (r1.identity.id, 1, 30, iso(now)))

    r2 = _drop(conn, g.key, c, 5, "adminuser", "admin_x", "2", 1, now)
    assert not r2.ok and r2.reason == "obligation_open"

    r3 = _drop(conn, g.key, c, 5, "adminuser", "admin_x", "2", 1, now, )
    assert r3.reason == "obligation_open"
    admin_ok = idm.gate_drop(
        conn, g.key, c.key, 1, 5, "adminuser", "admin_x", "2", 30, 4, now, is_admin=True,
    )
    assert admin_ok.ok, "admin bypasses the obligation gate"


def test_admin_skips_vip_cap(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 5, "vipadmin", "vipadmin_x", "1", 1, now)
    assert r.ok
    idm.set_vip(conn, 5, True)
    for i in range(4):
        dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (g.key, 1, c.key, i + 1, r.identity.id, r.account.id, "l", str(10 + i), iso(now)))
    blocked = idm.gate_drop(conn, g.key, c.key, 1, 5, "vipadmin", "vipadmin_x", "99", 30, 4, now)
    assert not blocked.ok and blocked.reason == "vip_limit"
    allowed = idm.gate_drop(conn, g.key, c.key, 1, 5, "vipadmin", "vipadmin_x", "99", 30, 4, now, is_admin=True)
    assert allowed.ok, "admin bypasses the VIP cap"


def test_admin_skips_duplicate_block(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 5, "dupadmin", "dupadmin_x", "77", 1, now)
    dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
           (g.key, 1, c.key, 1, r.identity.id, r.account.id, "l", "77", iso(now)))
    normal = idm.gate_drop(conn, g.key, c.key, 1, 5, "dupadmin", "dupadmin_x", "77", 30, 4, now)
    assert not normal.ok and normal.reason == "duplicate"
    admin_ok = idm.gate_drop(conn, g.key, c.key, 1, 5, "dupadmin", "dupadmin_x", "77", 30, 4, now, is_admin=True)
    assert admin_ok.ok


def test_admin_still_blocked_when_banned(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 5, "bannedadmin", "bannedadmin_x", "1", 1, now)
    dbm.ex(conn, "UPDATE identities SET blacklisted_at=? WHERE id=?", (iso(now), r.identity.id))
    res = idm.gate_drop(conn, g.key, c.key, 1, 5, "bannedadmin", "bannedadmin_x", "2", 30, 4, now, is_admin=True)
    assert not res.ok and res.reason == "banned", "safety gates still apply to admins"


def test_group_admin_sync_and_lookup(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    n = idm.sync_group_admins(conn, cfg.groups[0].key, cfg.groups[0].admin_user_ids)
    assert n == 1
    assert idm.is_admin_of(conn, cfg.groups[0].key, 5) is True
    assert idm.is_admin_of(conn, cfg.groups[0].key, 999) is False
    assert idm.is_admin_of(conn, cfg.groups[0].key, None) is False


# --- drop credits ----------------------------------------------------

def test_wrong_link_delete_grants_credit(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
           (r.identity.id, 1, 30, iso(now)))
    drop_id = _record(conn, g.key, c, r, "alice_x", "1", now)
    result = qb.delete_wrong_link(conn, c.key, 1, 5, "wrong link")
    assert result["blocked"] is None
    assert dbm.q1(conn, "SELECT status FROM drops WHERE id=?", (drop_id,))["status"] == "deleted_wrong_link"
    assert idm.unconsumed_credits(conn, r.identity.id) == 1

    again = idm.gate_drop(conn, g.key, c.key, 1, 111, "alice", "alice_x", "2", 30, 4, now)
    assert again.ok, "a credit lets the member re-drop despite the open obligation"


def test_credit_consumed_on_drop(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    idm.grant_drop_credit(conn, r.identity.id, 1, 1, "wrong link")
    assert idm.consume_drop_credit(conn, r.identity.id) is not None
    assert idm.unconsumed_credits(conn, r.identity.id) == 0
    assert idm.consume_drop_credit(conn, r.identity.id) is None


def test_credit_lifts_vip_cap_once(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 111, "vipbob", "vipbob_x", "1", 1, now)
    idm.set_vip(conn, 111, True)
    for i in range(4):
        dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
               (g.key, 1, c.key, i + 1, r.identity.id, r.account.id, "l", str(10 + i), iso(now)))
    blocked = idm.gate_drop(conn, g.key, c.key, 1, 111, "vipbob", "vipbob_x", "50", 30, 4, now)
    assert not blocked.ok
    idm.grant_drop_credit(conn, r.identity.id, 1, 1, "wrong link")
    allowed = idm.gate_drop(conn, g.key, c.key, 1, 111, "vipbob", "vipbob_x", "50", 30, 4, now)
    assert allowed.ok, "a credit lifts the VIP cap once"


def test_delete_blocked_when_case_open(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 111, "carol", "carol_x", "1", 1, now)
    drop_id = _record(conn, g.key, c, r, "carol_x", "1", now)
    dbm.ex(conn, "INSERT INTO cases (identity_id, drop_id, missing_count, evidence, status, opened_at) VALUES (?,?,?,'{}','open',?)",
           (r.identity.id, drop_id, 5, iso(now)))
    result = qb.delete_wrong_link(conn, c.key, 1, 5, "wrong link")
    assert result["blocked"] == "open_case"
    assert idm.unconsumed_credits(conn, r.identity.id) == 0, "no credit while evidence is open"


# --- bind rules ------------------------------------------------------

def test_first_drop_binds_handle_automatically(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    assert idm.identity_by_handle(conn, "auto_x") is None
    r = _drop(conn, g.key, c, 111, "auto", "auto_x", "1", 1, now)
    assert r.ok
    assert idm.identity_by_handle(conn, "auto_x") is not None
    assert r.identity.tg_username == "auto"


def test_member_cannot_rebind_own_handle(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    r = _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    assert r.identity.x_handle == "alice_x"
    assert "must ask admin" not in ""  # sanity
    # only rebind_handle (admin command) may change it
    changed = idm.rebind_handle(conn, 111, "alice_new")
    assert changed.x_handle == "alice_new"


def test_rebind_rejects_handle_owned_by_someone_else(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    _drop(conn, g.key, c, 222, "bob", "bob_x", "2", 1, now)
    try:
        idm.rebind_handle(conn, 111, "bob_x")
    except ValueError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_rebind_without_identity_raises_lookup(tmp_path):
    conn = connect(tmp_path)
    try:
        idm.rebind_handle(conn, 999, "ghost_x")
    except LookupError:
        pass
    else:
        raise AssertionError("expected LookupError")


def test_rebind_is_audited(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    idm.rebind_handle(conn, 111, "alice_v2")
    row = dbm.q1(conn, "SELECT * FROM external_actions WHERE kind='rebind'")
    assert row is not None
    assert "alice_x -> alice_v2" in row["raw_text"]


def test_rebind_same_handle_is_noop(tmp_path):
    conn = connect(tmp_path)
    cfg = _cfg(tmp_path)
    g, c = cfg.groups[0], cfg.groups[0].circles[0]
    now = dt(2026, 1, 1, 12)
    _drop(conn, g.key, c, 111, "alice", "alice_x", "1", 1, now)
    same = idm.rebind_handle(conn, 111, "alice_x")
    assert same.x_handle == "alice_x"
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM external_actions WHERE kind='rebind'")["n"] == 0