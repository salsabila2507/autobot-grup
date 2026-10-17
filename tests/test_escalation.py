from datetime import timedelta

from engine import cards
from engine import db as dbm
from engine import escalation
from engine import identity as idm
from engine import outbox
from engine import punishment as pm
from tests.helpers import connect, dt, iso, rules, write_config
from engine import config as cfgmod
from services import scheduler as sched


def _seed_identity(conn, tg_id=111, handle="alice_x", username="alice"):
    ident, _ = idm.bind_handle(conn, tg_id, username, handle)
    return ident


def _drop(conn, ident, position=1, round_id=1, tweet_id="1", created=None):
    return dbm.ex(
        conn,
        "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("hotti", round_id, "eng_blue", position, ident.id, 1, "l", tweet_id,
         (created or dt(2026, 1, 1, 12)).isoformat(timespec="seconds")),
    )


def _obligation(conn, ident, drop_id, required=30, created=None):
    return dbm.ex(
        conn,
        "INSERT INTO obligations (identity_id, drop_id, required, created_at) VALUES (?,?,?,?)",
        (ident.id, drop_id, required, (created or dt(2026, 1, 1, 10)).isoformat(timespec="seconds")),
    )


def _open_case(conn, ident, drop_id, missing=3, opened=None):
    return dbm.ex(
        conn,
        "INSERT INTO cases (identity_id, drop_id, missing_count, evidence, status, opened_at) VALUES (?,?,?,'{}','open',?)",
        (ident.id, drop_id, missing, (opened or dt(2026, 1, 1, 12)).isoformat(timespec="seconds")),
    )


def test_schema_tables_exist(tmp_path):
    conn = connect(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "punish_intents" in names
    assert "outbox" in names
    assert "drop_credits" in names
    assert "group_admins" in names
    v = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert v == 5


def test_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path)
    dbm.migrate(conn)
    dbm.migrate(conn)
    dbm.migrate(conn)
    v = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert v == 5


def test_outbox_roundtrip(tmp_path):
    conn = connect(tmp_path)
    oid = outbox.enqueue(conn, "hotti", "divider", {"text": "hi", "chat_id": -1})
    rows = outbox.pending(conn)
    assert len(rows) == 1
    assert outbox.payload_of(rows[0])["text"] == "hi"
    outbox.mark_sent(conn, oid)
    assert outbox.pending(conn) == []


def test_outbox_failed_after_five_attempts(tmp_path):
    conn = connect(tmp_path)
    oid = outbox.enqueue(conn, "hotti", "divider", {"text": "x"})
    for _ in range(5):
        outbox.bump_attempt(conn, oid, "boom")
    assert outbox.pending(conn) == []


def test_queue_punish_and_drain(tmp_path):
    conn = connect(tmp_path)
    pid = outbox.queue_punish(conn, 1, 5, {"kind": "warn", "level": 1, "duration": "4h", "reason": "r", "mode": "bot_auto"})
    rows = outbox.pending_punish(conn)
    assert len(rows) == 1
    assert rows[0]["kind"] == "warn"
    outbox.mark_punish_sent(conn, pid)
    assert outbox.pending_punish(conn) == []


def test_cases_past_deadline(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did, opened=dt(2026, 1, 1, 12, 0))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    r = rules(enforce_deadline_minutes=15)
    now = dt(2026, 1, 1, 12, 10)
    assert escalation.cases_past_deadline(conn, "hotti", r, now) == []
    now2 = dt(2026, 1, 1, 12, 20)
    rows = escalation.cases_past_deadline(conn, "hotti", r, now2)
    assert [x["id"] for x in rows] == [cid]


def test_escalate_case_writes_punishment_and_intent(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    r = rules()
    result = escalation.escalate_case(conn, group, r, case_row, mode="bot_auto", now=dt(2026, 1, 1, 13))
    assert result is not None
    assert result["level"] == 1
    p = dbm.q1(conn, "SELECT * FROM punishments WHERE case_id=?", (cid,))
    assert p["kind"] == "warn"
    assert p["mode"] == "bot_auto"
    intents = outbox.pending_punish(conn)
    assert len(intents) == 1
    assert intents[0]["level"] == 1


def test_escalate_case_respects_max_warnings(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    dbm.ex(conn, "UPDATE identities SET strikes=3 WHERE id=?", (ident.id,))
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    assert escalation.escalate_case(conn, group, rules(), case_row, "bot_auto", dt(2026, 1, 1, 13)) is None


def test_escalate_case_uses_strike_mirror(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    dbm.ex(conn, "UPDATE identities SET strikes=1 WHERE id=?", (ident.id,))
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    result = escalation.escalate_case(conn, group, rules(), case_row, "list_then_auto", dt(2026, 1, 1, 13))
    assert result["level"] == 2
    assert result["duration"] == "48h"


def test_actioned_case_survives_for_appeal_and_the_card(tmp_path):
    """The case must not be closed by the punishment.

    Closing it here is what killed two flows at once: /appeal looks for an open case,
    and the card buttons refuse anything that is not open, so a punished member could
    never appeal and an admin could never clear or confirm. It also must not stay
    'open', because then the scheduler would escalate it again every tick.
    """
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]

    escalation.escalate_case(conn, group, rules(), case_row, "bot_auto", dt(2026, 1, 1, 13))

    row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    assert row["status"] == dbm.CASE_ACTIONED, row["status"]
    # appeal still finds it
    appealable = dbm.q1(conn, "SELECT id FROM cases WHERE identity_id=? AND status IN (?,?)",
                        (ident.id, dbm.CASE_OPEN, dbm.CASE_ACTIONED))
    assert appealable["id"] == cid
    # the admin card still works on it
    ok = cards.apply_callback(conn, f"case:{cid}:clear", 999, [999])
    assert ok["ok"] is True, ok
    assert dbm.q1(conn, "SELECT status FROM cases WHERE id=?", (cid,))["status"] == \
        dbm.CASE_CLEARED


def test_actioned_case_is_not_escalated_again(tmp_path):
    """The whole reason it cannot stay 'open': one punishment per case, not one per tick."""
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    now = dt(2026, 1, 1, 13)

    escalation.escalate_case(conn, group, rules(), case_row, "bot_auto", now)
    rules_obj = rules()
    deadline = now - timedelta(minutes=rules_obj.enforce_deadline_minutes)
    again = [
        r for r in escalation.cases_past_deadline(conn, group.key, rules_obj, deadline)
        if r["id"] == cid
    ]
    assert again == [], "an actioned case must not be escalated a second time"


def test_escalate_case_not_twice(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    cid = _open_case(conn, ident, did)
    case_row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    group = cfgmod.load(write_config(str(tmp_path / "c.yaml"))).groups[0]
    escalation.escalate_case(conn, group, rules(), case_row, "bot_auto", dt(2026, 1, 1, 13))
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM punish_intents")["n"] == 1
    # cases_past_deadline no longer sees the closed case
    assert escalation.cases_past_deadline(conn, "hotti", rules(enforce_deadline_minutes=0), dt(2026, 1, 2)) == []


def test_boundary_warning_writes_once(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident, created=dt(2026, 1, 1, 10))
    _obligation(conn, ident, did)
    rows = escalation.open_obligations_before(conn, before=dt(2026, 1, 1, 12).isoformat(timespec="seconds"))
    assert len(rows) == 1
    result = escalation.boundary_warning(conn, "hotti", rules(), rows[0], "2026-01-01T12:00:00+00:00", dt(2026, 1, 1, 12))
    assert result is not None
    assert result["level"] == 1
    again = escalation.boundary_warning(conn, "hotti", rules(), rows[0], "2026-01-01T12:00:00+00:00", dt(2026, 1, 1, 12))
    assert again is None


def test_boundary_warning_skips_vip(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident, created=dt(2026, 1, 1, 10))
    _obligation(conn, ident, did)
    idm.set_vip(conn, 111, True)
    rows = escalation.open_obligations_before(conn, before=dt(2026, 1, 1, 12).isoformat(timespec="seconds"))
    assert escalation.boundary_warning(conn, "hotti", rules(), rows[0], "b", dt(2026, 1, 1, 12)) is None


def test_boundary_warning_respects_max(tmp_path):
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident, created=dt(2026, 1, 1, 10))
    _obligation(conn, ident, did)
    dbm.ex(conn, "UPDATE identities SET strikes=3 WHERE id=?", (ident.id,))
    rows = escalation.open_obligations_before(conn, before=dt(2026, 1, 1, 12).isoformat(timespec="seconds"))
    assert escalation.boundary_warning(conn, "hotti", rules(), rows[0], "b", dt(2026, 1, 1, 12)) is None


def test_scheduler_tick_enqueues_divider_once(tmp_path):
    conn = connect(tmp_path)
    cfg = cfgmod.load(write_config(str(tmp_path / "c.yaml")))
    s = sched.Scheduler(cfg, conn)
    now = dt(2026, 1, 1, 6, 5)
    first = s.tick(now)
    assert first["dividers"] == 1
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM outbox WHERE kind='divider'")["n"] == 1
    second = s.tick(now)
    assert second["dividers"] == 0
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM outbox WHERE kind='divider'")["n"] == 1


def test_scheduler_new_boundary_posts_again(tmp_path):
    conn = connect(tmp_path)
    cfg = cfgmod.load(write_config(str(tmp_path / "c.yaml")))
    s = sched.Scheduler(cfg, conn)
    s.tick(dt(2026, 1, 1, 6, 5))
    s.tick(dt(2026, 1, 1, 18, 5))
    assert dbm.q1(conn, "SELECT COUNT(*) AS n FROM outbox WHERE kind='divider'")["n"] == 2


def test_scheduler_boundary_warning_public_line(tmp_path):
    conn = connect(tmp_path)
    cfg = cfgmod.load(write_config(str(tmp_path / "c.yaml")))
    ident = _seed_identity(conn)
    did = _drop(conn, ident, created=dt(2026, 1, 1, 10))
    _obligation(conn, ident, did)
    s = sched.Scheduler(cfg, conn)
    stats = s.tick(dt(2026, 1, 1, 18, 5))
    assert stats["warnings"] == 1
    row = dbm.q1(conn, "SELECT * FROM outbox WHERE kind='public_warning'")
    assert row is not None
    assert "@alice" in outbox.payload_of(row)["text"]
    assert row["payload"]


def test_scheduler_divider_payload_has_rules_link(tmp_path):
    conn = connect(tmp_path)
    cfg = cfgmod.load(write_config(str(tmp_path / "c.yaml")))
    sched.Scheduler(cfg, conn).tick(dt(2026, 1, 1, 6, 5))
    row = dbm.q1(conn, "SELECT * FROM outbox WHERE kind='divider'")
    payload = outbox.payload_of(row)
    assert payload["text"].startswith("Quick engagement here")
    assert "read rules" in payload["text"]
    assert payload["topic_id"] == 4

def test_boundary_warning_yields_to_an_open_case(tmp_path):
    """One missed engagement, one warning.

    The boundary warning used to fire regardless, so a member whose drop had already
    been actioned through a case was warned twice for the same miss, which the locked
    rule "every open obligation produces exactly one warning" forbids.
    """
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                 "VALUES (?,?,?,?)", (ident.id, did, 30, iso(dt(2026, 1, 1, 8))))
    ob = dbm.q1(conn, "SELECT * FROM obligations WHERE identity_id=?", (ident.id,))

    cid = _open_case(conn, ident, did)
    assert dbm.q1(conn, "SELECT status FROM cases WHERE id=?", (cid,))["status"] == "open"

    result = escalation.boundary_warning(conn, "g", rules(), ob,
                                         iso(dt(2026, 1, 1, 12)), dt(2026, 1, 1, 12))
    assert result is None, "an open case already owns this miss"
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punishments")["n"] == 0


def test_boundary_warning_still_fires_with_no_case(tmp_path):
    """Removing the double-warning must not silence the boundary warning entirely."""
    conn = connect(tmp_path)
    ident = _seed_identity(conn)
    did = _drop(conn, ident)
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                 "VALUES (?,?,?,?)", (ident.id, did, 30, iso(dt(2026, 1, 1, 8))))
    ob = dbm.q1(conn, "SELECT * FROM obligations WHERE identity_id=?", (ident.id,))

    result = escalation.boundary_warning(conn, "g", rules(), ob,
                                         iso(dt(2026, 1, 1, 12)), dt(2026, 1, 1, 12))
    assert result is not None
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punishments")["n"] == 1
