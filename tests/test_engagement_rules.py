"""The engagement rules, exercised through the path that actually runs.

These used to test the old full-window scorer, which nothing calls any more. The rules
themselves still matter, so they are checked here against the sampled checker:

  a required link counts only when the member both reacted with the heart and
  commented on X
  a likes circle is judged on the heart alone and never opens X
  a partial read is a question for a human, never a defaulter
  a violation has to be confirmed on a second, disjoint sample
"""

import random

from engine import checker
from engine import db as dbm
from engine import identity as idm
from engine import lookup
from engine import rounds as rounds_mod
from engine import sampling
from tests.helpers import connect, circle, dt, iso

SCHEDULE = ["06:00-18:00", "18:00-06:00"]
TARGET_TG = 705


def _setup(tmp_path, n=6):
    conn = connect(tmp_path)
    c = circle(key="eng_blue", topic_id=15, required=10)
    now = dt(2026, 1, 1, 12)
    rid = rounds_mod.current_round(conn, c.key, SCHEDULE, now).id
    ids = {}
    for i in range(n):
        ident, _ = idm.bind_handle(conn, 700 + i, f"dropper{i}", f"dp{i}_x")
        ids[700 + i] = ident.id
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
            "account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("g", rid, c.key, i + 1, ident.id, ident.id, "l", f"tweet{i}",
             iso(dt(2026, 1, 1, 9, i))),
        )
    return conn, c, ids


def _window(conn, c, tg=TARGET_TG):
    ident = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=?", (tg,))["identity_id"]
    return ident, lookup.links_above(conn, lookup.latest_drop(conn, ident), c)


def _heart(conn, rows, tg=TARGET_TG):
    for r in rows:
        dbm.ex(
            conn,
            "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
            "VALUES (?,?,?,?)",
            (r["id"], tg, "\u2764", dbm.now_iso()),
        )


def _reader(reply_handles, complete=True, closed=()):
    closed = set(closed)

    def reader(tweet_id, owner_handle=None, member_handle=None):
        if tweet_id in closed:
            return {"tweet_id": tweet_id, "url": None, "replied": None,
                    "complete": False, "open": False}
        if not complete:
            return {"tweet_id": tweet_id, "url": None, "replied": None,
                    "complete": False, "open": True}
        return {
            "tweet_id": tweet_id,
            "url": f"https://x.com/{owner_handle}/status/{tweet_id}",
            "replied": (member_handle or "") in reply_handles,
            "complete": True,
            "open": True,
        }

    return reader


def test_both_signals_counts_as_engaged(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    _heart(conn, rows)
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader({handle}))
    assert res["verdict"] == "clear"


def test_reaction_without_a_comment_is_a_miss(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    _heart(conn, rows)
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader(set()))
    assert res["verdict"] == "violated"
    assert res["confirmed"] is True


def test_comment_without_a_reaction_is_a_miss(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader({handle}))
    assert res["verdict"] == "violated"


def test_reaction_by_another_member_does_not_count(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    for r in rows:
        dbm.ex(
            conn,
            "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
            "VALUES (?,?,?,?)",
            (r["id"], 999, "\u2764", dbm.now_iso()),
        )
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader({handle}))
    assert res["verdict"] == "violated"


def test_only_the_heart_counts_as_a_reaction(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    for r in rows:
        dbm.ex(
            conn,
            "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
            "VALUES (?,?,?,?)",
            (r["id"], TARGET_TG, "\U0001F44D", dbm.now_iso()),
        )
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader({handle}))
    assert res["verdict"] == "violated"


def test_unreadable_thread_goes_to_review_not_a_case(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    _heart(conn, rows)
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0),
                                    _reader(set(), complete=False))
    assert res["verdict"] == "review"
    assert sampling.open_case_for(conn, res, type("R", (), {"miss_threshold": 2}), ident) is None


def test_closed_comments_never_produce_a_case(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    closed = {r["tweet_id"] for r in rows}
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0),
                                    _reader(set(), closed=closed))
    assert res["verdict"] == "review"
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases")["n"] == 0


def test_confirmed_violation_opens_exactly_one_case(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader(set()))
    first = sampling.open_case_for(conn, res, type("R", (), {"miss_threshold": 2}), ident)
    second = sampling.open_case_for(conn, res, type("R", (), {"miss_threshold": 2}), ident)
    assert first and first == second
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases")["n"] == 1


def test_second_sample_clearing_downgrades_to_review(tmp_path):
    conn, c, _ = _setup(tmp_path)
    ident, rows = _window(conn, c)
    _heart(conn, rows)
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]

    class ClearsOnSecondRead:
        """The member genuinely engaged, sampling luck just hid it at first."""

        def __init__(self):
            self.calls = 0

        def __call__(self, tweet_id, owner_handle=None, member_handle=None):
            self.calls += 1
            replied = self.calls > 2
            return {
                "tweet_id": tweet_id,
                "url": f"https://x.com/{owner_handle}/status/{tweet_id}",
                "replied": replied,
                "complete": True,
                "open": True,
            }

    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), ClearsOnSecondRead())
    assert res["verdict"] == "review"
    assert res["confirmed"] is False
    assert handle


def test_likes_circle_is_judged_on_the_heart_alone(tmp_path):
    conn = connect(tmp_path)
    c = circle(key="like20", topic_id=16, kind="likes", required=10)
    rid = rounds_mod.current_round(conn, c.key, SCHEDULE, dt(2026, 1, 1, 12)).id
    for i in range(5):
        ident, _ = idm.bind_handle(conn, 800 + i, f"liker{i}", f"lk{i}_x")
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
            "account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("g", rid, c.key, i + 1, ident.id, ident.id, "l", f"tweet{i}",
             iso(dt(2026, 1, 1, 9, i))),
        )
    ident = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=804")["identity_id"]
    rows = lookup.links_above(conn, lookup.latest_drop(conn, ident), c)
    _heart(conn, rows, tg=804)

    calls = []

    def reader(tweet_id, owner_handle=None, member_handle=None):
        calls.append(tweet_id)
        return {"tweet_id": tweet_id, "url": None, "replied": False,
                "complete": True, "open": True}

    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), reader)
    assert res["verdict"] == "clear"
    assert calls == [], "a likes circle must never open X"


def test_obligation_stays_open_while_a_miss_remains(tmp_path):
    conn, c, ids = _setup(tmp_path)
    ident = ids[TARGET_TG]
    did = dbm.q1(conn, "SELECT id FROM drops WHERE identity_id=?", (ident,))["id"]
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                 "VALUES (?,?,?,?)", (ident, did, 30, dbm.now_iso()))
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader(set()))
    assert res["verdict"] == "violated"
    assert checker.verdict([checker.MISS, checker.MISS]) == "violated"
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations WHERE cleared_at IS NULL")["n"] == 1


def test_obligation_clears_when_the_member_completes(tmp_path):
    conn, c, ids = _setup(tmp_path)
    from engine import identity as identity_mod

    ident = ids[TARGET_TG]
    did = dbm.q1(conn, "SELECT id FROM drops WHERE identity_id=?", (ident,))["id"]
    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                 "VALUES (?,?,?,?)", (ident, did, 30, dbm.now_iso()))
    _, rows = _window(conn, c)
    _heart(conn, rows)
    handle = dbm.q1(conn, "SELECT x_handle FROM identities WHERE id=?", (ident,))["x_handle"]
    res = sampling.evaluate_sampled(conn, ident, c, 2, random.Random(0), _reader({handle}))
    assert res["verdict"] == "clear"
    assert identity_mod.reset_member(conn, ident)["obligations"] == 1
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations WHERE cleared_at IS NULL")["n"] == 0