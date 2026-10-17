"""Checker lookup and sampled judging.

The checker never needs queue positions. It resolves a username, walks back to that
member's latest drop, collects the links above it, and samples a few of those.
"""

import json
import random

from engine import db as dbm
from engine import identity as idm
from engine import lookup
from engine import rounds as rounds_mod
from engine import sampling
from tests.helpers import connect, circle, dt, iso

SCHEDULE = ["06:00-18:00", "18:00-06:00"]
CIRCLE = dict(key="eng_blue", topic_id=15, kind="engagement", required=5)


def _seed(tmp_path, n=10):
    conn = connect(tmp_path)
    c = circle(key=CIRCLE["key"], topic_id=15, required=CIRCLE["required"])
    now = dt(2026, 1, 1, 12)
    rid = rounds_mod.current_round(conn, c.key, SCHEDULE, now).id
    for i in range(n):
        ident, _ = idm.bind_handle(conn, 800 + i, f"dropper{i}", f"dp{i}_x")
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("g", rid, c.key, i + 1, ident.id, ident.id, "l", f"tweet{i}", iso(dt(2026, 1, 1, 9, i))),
        )
    return conn, c


def _last_identity(conn, tg_id):
    row = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=?", (tg_id,))
    return row["identity_id"]


def _react(conn, drop_id, tg_id):
    dbm.ex(
        conn,
        "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) VALUES (?,?,?,?)",
        (drop_id, tg_id, "❤", iso(dt(2026, 1, 1, 11))),
    )


def _comment(conn, tweet_id, handle):
    dbm.ex(
        conn,
        "INSERT INTO engagement_reads (tweet_id, handle, x_user_id, kind, read_at, account_used) VALUES (?,?,?,?,?,?)",
        (tweet_id, handle, None, "reply", iso(dt(2026, 1, 1, 11)), "acct01"),
    )


# --- lookup ----------------------------------------------------------

def test_find_identity_by_telegram_username(tmp_path):
    conn, c = _seed(tmp_path)
    found = lookup.find_identity(conn, "dropper3")
    assert found == _last_identity(conn, 803)


def test_find_identity_by_x_handle(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "dp3_x") == _last_identity(conn, 803)


def test_find_identity_with_at_prefix(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "@dp3_x") == _last_identity(conn, 803)


def test_find_identity_case_insensitive(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "DROPPER3") == _last_identity(conn, 803)


def test_find_identity_unknown_returns_none(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "nobody") is None
    assert lookup.find_identity(conn, "") is None
    assert lookup.find_identity(conn, None) is None


def test_latest_drop_is_the_judged_one(tmp_path):
    conn, c = _seed(tmp_path)
    drop = lookup.latest_drop(conn, _last_identity(conn, 807))
    assert drop["position"] == 8


def test_links_above_excludes_own_drop(tmp_path):
    conn, c = _seed(tmp_path)
    drop = lookup.latest_drop(conn, _last_identity(conn, 807))
    above = lookup.links_above(conn, drop, c)
    assert len(above) == CIRCLE["required"]
    assert all(r["position"] < drop["position"] for r in above)


def test_links_above_is_newest_first(tmp_path):
    conn, c = _seed(tmp_path)
    drop = lookup.latest_drop(conn, _last_identity(conn, 807))
    positions = [r["position"] for r in lookup.links_above(conn, drop, c)]
    assert positions == sorted(positions, reverse=True)


def test_links_above_respects_circle_required(tmp_path):
    conn, c = _seed(tmp_path, n=20)
    drop = lookup.latest_drop(conn, _last_identity(conn, 819))
    assert len(lookup.links_above(conn, drop, c)) == 5


def test_tweet_url(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.tweet_url("alice", "123") == "https://x.com/alice/status/123"
    assert lookup.tweet_url("@alice", "123") == "https://x.com/alice/status/123"




def test_sample_links_returns_no_more_than_size(tmp_path):
    conn, c = _seed(tmp_path, n=20)
    rng = random.Random(1)
    result = lookup.sample_links(conn, _last_identity(conn, 819), c, 2, rng)
    assert len(result["sample"]) == 2


def test_sample_links_when_no_drops(tmp_path):
    conn, c = _seed(tmp_path)
    result = lookup.sample_links(conn, _last_identity(conn, 800), c, 2, random.Random(0))
    # dropper0 has a drop but there is nothing above position 1
    assert result["sample"] == []


# --- judging ---------------------------------------------------------

def test_judge_all_engaged_is_clear():
    assert sampling.judge([True, True]) == "clear"


def test_judge_all_missing_is_violated():
    assert sampling.judge([False, False]) == "violated"


def test_judge_split_is_mixed():
    assert sampling.judge([True, False]) == "mixed"
    assert sampling.judge([False, True]) == "mixed"


def test_judge_empty_is_unknown():
    assert sampling.judge([]) == "unknown"




















def test_verdict_line_readable(tmp_path):
    line = sampling.verdict_line({"verdict": "clear", "checked": 2, "window_total": 30})
    assert "2/2" in line
    assert "PASS" in line






def test_build_card_handles_missing_identity(tmp_path):
    result = {"verdict": "unknown", "reason": "no drop yet", "links": [], "checked": 0, "window_total": 0}
    text_value, buttons = sampling.build_card(result)
    assert buttons == []
    assert "Sampled check" in text_value

# --- resolution must never land on the wrong person -------------------

def test_partial_username_does_not_resolve(tmp_path):
    """`/chek i` used to resolve to whoever sorted first, leaking their data."""
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "bud") is None
    assert lookup.find_identity(conn, "i") is None
    assert lookup.find_identity(conn, "ropper") is None


def test_partial_x_handle_does_not_resolve(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "dp3") is None


def test_exact_still_resolves(tmp_path):
    conn, c = _seed(tmp_path)
    assert lookup.find_identity(conn, "dropper3") == _last_identity(conn, 803)
    assert lookup.find_identity(conn, "dp3_x") == _last_identity(conn, 803)


def test_find_identities_reports_every_exact_match(tmp_path):
    conn, c = _seed(tmp_path)
    assert len(lookup.find_identities(conn, "dropper3")) == 1
    assert lookup.find_identities(conn, "nothing") == []


# --- a confirmed violation has to become a real case -------------------

class _Rules:
    miss_threshold = 2


def _confirmed_violation(conn, c):
    return sampling.evaluate_sampled(conn, _last_identity(conn, 807), c, 2,
                                     random.Random(0), _MissEverything())


class _MissEverything:
    def __call__(self, tweet_id, owner_handle=None, member_handle=None):
        return {"tweet_id": tweet_id, "url": f"https://x.com/{owner_handle}/status/{tweet_id}",
                "replied": False, "complete": True, "open": True}


def test_confirmed_violation_opens_a_case(tmp_path):
    conn, c = _seed(tmp_path)
    result = _confirmed_violation(conn, c)
    assert result["verdict"] == "violated"
    case_id = sampling.open_case_for(conn, result, _Rules(), result["identity"]["id"])
    assert case_id
    row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (case_id,))
    assert row["status"] == "open"
    assert json.loads(row["evidence"])["method"] == "sampled"


def test_review_verdict_never_opens_a_case(tmp_path):
    conn, c = _seed(tmp_path)
    _react_all_sample(conn, c)
    result = sampling.evaluate_sampled(conn, _last_identity(conn, 807), c, 2,
                                       random.Random(0), _PartialReader())
    assert result["verdict"] != "violated"
    assert sampling.open_case_for(conn, result, _Rules(), result["identity"]["id"]) is None
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases")["n"] == 0


def test_case_is_not_opened_twice(tmp_path):
    conn, c = _seed(tmp_path)
    result = _confirmed_violation(conn, c)
    first = sampling.open_case_for(conn, result, _Rules(), result["identity"]["id"])
    second = sampling.open_case_for(conn, result, _Rules(), result["identity"]["id"])
    assert first == second
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases")["n"] == 1


class _PartialReader:
    def __call__(self, tweet_id, owner_handle=None, member_handle=None):
        return {"tweet_id": tweet_id, "url": None, "replied": None,
                "complete": False, "open": True}


def _react_all_sample(conn, c, tg=807):
    member = _last_identity(conn, tg)
    for row in lookup.links_above(conn, lookup.latest_drop(conn, member), c):
        dbm.ex(conn, "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, "
                     "observed_at) VALUES (?,?,?,?)", (row["id"], tg, "\u2764", dbm.now_iso()))
