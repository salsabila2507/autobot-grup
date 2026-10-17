"""Offline acceptance checks mapped 1:1 to the Phase 1 acceptance list in the plan.

These assert engine behaviour only. Telegram side effects (message deleted, entry
posted) are verified in the sandbox, not here.
"""

from engine import config as cfgmod
from engine import db as dbm
from engine import identity as idm
from engine import numbering
from engine import rounds as rounds_mod
from services import queue_bot as qb
from tests.helpers import connect, dt, iso, write_config

SCHEDULE = ["06:00-18:00", "18:00-06:00"]


class FakeMsg:
    def __init__(self, text, sender_id, username=None, reply_to_topic=None):
        self.text = text
        self.message = text
        self.sender_id = sender_id
        self.sender = type("U", (), {"username": username, "bot": False})()
        self.id = 1000 + sender_id
        self.chat_id = -1001
        self._reply_to_topic = reply_to_topic

    @property
    def reply_to(self):
        if self._reply_to_topic is None:
            return None
        return type("H", (), {"forum_topic": True, "_": self._reply_to_topic})()

    @property
    def reply_to_msg_id(self):
        return self._reply_to_topic


def _ctx(tmp_path):
    cfg = cfgmod.load(write_config(str(tmp_path / "c.yaml")))
    conn = connect(tmp_path)
    group = cfg.groups[0]
    circle = group.circles[0]
    return cfg, conn, group, circle


def _drop(conn, group, circle, tg, user, handle, tweet, now, vip=4, admin=False):
    rid = rounds_mod.current_round(conn, circle.key, SCHEDULE, now).id
    return idm.gate_drop(
        conn, group.key, circle.key, rid, tg, user, handle, tweet, circle.required, vip, now, is_admin=admin,
    )


def _commit(conn, group, circle, res, handle, tweet, now):
    intent = {
        "group_key": group.key,
        "round_id": rounds_mod.current_round(conn, circle.key, SCHEDULE, now).id,
        "identity": res.identity,
        "account": res.account,
        "position": numbering.next_position(conn, circle.key),
    }
    return qb.commit_drop(conn, circle, intent, handle, tweet, now)


def test_five_valid_links_produce_entries_1_to_5(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    for i in range(1, 6):
        h, t = f"member{i}", str(100 + i)
        res = _drop(conn, group, circle, 100 + i, h, h + "_x", t, now)
        assert res.ok, res.reason
        _commit(conn, group, circle, res, h + "_x", t, now)
    positions = [r["position"] for r in dbm.q(conn, "SELECT position FROM drops ORDER BY position")]
    assert positions == [1, 2, 3, 4, 5]


def test_internal_status_link_rejected_specifically(tmp_path):
    reason = numbering.link_reject_reason("https://x.com/i/status/1234567890123")
    assert reason == "internal_status", "the classic Ape rule failure"


def test_non_link_rejected_without_consuming_position(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    before = numbering.next_position(conn, circle.key)
    assert before == 1
    reason = numbering.link_reject_reason("just some chatter")
    assert reason == "bad_format"
    assert numbering.next_position(conn, circle.key) == 1, "a rejected message consumes nothing"


def test_duplicate_same_member_same_round_rejected(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    r1 = _drop(conn, group, circle, 111, "alice", "alice_x", "555", now)
    _commit(conn, group, circle, r1, "alice_x", "555", now)
    # the first drop creates an open obligation, which the gate checks first
    blocked = _drop(conn, group, circle, 111, "alice", "alice_x", "555", now)
    assert not blocked.ok
    assert blocked.reason == "obligation_open"
    idm.clear_obligations(conn, r1.identity.id)
    r2 = _drop(conn, group, circle, 111, "alice", "alice_x", "555", now)
    assert not r2.ok and r2.reason == "duplicate"


def test_vip_fifth_drop_in_24h_rejected(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    first = _drop(conn, group, circle, 111, "vipbob", "vipbob_x", "1", now)
    assert first.ok
    idm.set_vip(conn, 111, True)
    for i in range(4):
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group.key, 1, circle.key, i + 1, first.identity.id, first.account.id, "l", str(10 + i), iso(now)),
        )
    over = _drop(conn, group, circle, 111, "vipbob", "vipbob_x", "99", now)
    assert not over.ok and over.reason == "vip_limit"
    positions = [r["position"] for r in dbm.q(conn, "SELECT position FROM drops ORDER BY position")]
    assert numbering.next_position(conn, circle.key) == 5, "the rejected VIP drop consumed nothing"


def test_normal_member_still_drops_while_vip_is_capped(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    vip = _drop(conn, group, circle, 111, "vipbob", "vipbob_x", "1", now)
    idm.set_vip(conn, 111, True)
    for i in range(4):
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (group.key, 1, circle.key, i + 1, vip.identity.id, vip.account.id, "l", str(10 + i), iso(now)),
        )
    normal = _drop(conn, group, circle, 222, "carol", "carol_x", "77", now)
    assert normal.ok


def test_deleted_entry_leaves_gap_and_counter_continues(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    for i in range(1, 4):
        res = _drop(conn, group, circle, 100 + i, f"m{i}", f"m{i}_x", str(i), now)
        _commit(conn, group, circle, res, f"m{i}_x", str(i), now)
    qb.delete_wrong_link(conn, circle.key, 2, 5, "wrong link")
    assert numbering.next_position(conn, circle.key) == 4, "the gap is never reused"
    row = dbm.q1(conn, "SELECT status FROM drops WHERE position=2")
    assert row["status"] == "deleted_wrong_link"


def test_counter_survives_restart_mid_round(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    now = dt(2026, 1, 1, 12)
    for i in range(1, 4):
        res = _drop(conn, group, circle, 100 + i, f"m{i}", f"m{i}_x", str(i), now)
        _commit(conn, group, circle, res, f"m{i}_x", str(i), now)

    # simulate restart: brand new connection to the same file, no in memory state
    conn2 = dbm.connect(str(tmp_path / "engine.sqlite"))
    assert numbering.next_position(conn2, circle.key) == 4, "state comes from the DB, not memory"
    res = idm.gate_drop(conn2, group.key, circle.key, 1, 104, "m4", "m4_x", "4", 30, 4, now)
    assert res.ok
    _commit(conn2, group, circle, res, "m4_x", "4", now)
    assert numbering.next_position(conn2, circle.key) == 5


def test_round_boundary_keeps_counter_running(tmp_path):
    cfg, conn, group, circle = _ctx(tmp_path)
    day = dt(2026, 1, 1, 12)
    for i in range(1, 3):
        res = _drop(conn, group, circle, 100 + i, f"m{i}", f"m{i}_x", str(i), day)
        _commit(conn, group, circle, res, f"m{i}_x", str(i), day)
    night = dt(2026, 1, 1, 19)
    r1 = rounds_mod.current_round(conn, circle.key, SCHEDULE, day)
    r2 = rounds_mod.current_round(conn, circle.key, SCHEDULE, night)
    assert r1.id != r2.id
    assert numbering.next_position(conn, circle.key) == 3, "the per circle counter never resets"
    res = _drop(conn, group, circle, 103, "m3", "m3_x", "3", night)
    _commit(conn, group, circle, res, "m3_x", "3", night)
    assert dbm.q1(conn, "SELECT position FROM drops ORDER BY position DESC LIMIT 1")["position"] == 3