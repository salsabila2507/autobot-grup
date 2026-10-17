"""Safety rules for judging a member.

These are the rules that decide whether a real person gets punished, so each one is
pinned down here: a partial read is never a miss, a closed thread is never judgeable,
and a single unknown on a sampled link forces human review.
"""

import random

import pytest

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from engine import checker
from engine import db as dbm
from engine import identity as idm
from engine import lookup
from engine import numbering
from engine import sampling
from engine import text as text_mod
from engine.x_reader import probe
from tests.helpers import circle, connect, dt, iso


# --- link_state -------------------------------------------------------

def test_both_signals_present_is_engaged():
    assert checker.link_state(True, True, True, True) == checker.ENGAGED


def test_complete_read_without_comment_is_a_miss():
    assert checker.link_state(True, False, True, True) == checker.MISS


def test_missing_reaction_is_a_miss_even_without_an_x_read():
    """Telegram reactions are authoritative on their own."""
    assert checker.link_state(False, None, True, False) == checker.MISS


def test_partial_read_is_never_a_miss():
    """This is the rule that stops a slow page from inventing defaulter cases."""
    assert checker.link_state(True, None, True, False) == checker.UNKNOWN


def test_unread_reply_is_unknown():
    assert checker.link_state(True, None, True, True) == checker.UNKNOWN


def test_closed_comments_are_never_judgeable():
    assert checker.link_state(True, None, False, False) == checker.UNKNOWN
    assert checker.link_state(False, False, False, True) == checker.UNKNOWN


# --- verdict ----------------------------------------------------------

def test_all_engaged_is_clear():
    assert checker.verdict([checker.ENGAGED, checker.ENGAGED]) == "clear"


def test_all_miss_is_defaulter():
    assert checker.verdict([checker.MISS, checker.MISS]) == "violated"


def test_mixed_goes_to_review():
    assert checker.verdict([checker.ENGAGED, checker.MISS]) == "review"


def test_one_unknown_forces_review_even_when_the_rest_missed():
    """The most important guard: a partial read can never auto-punish."""
    assert checker.verdict([checker.MISS, checker.UNKNOWN]) == "review"


def test_all_unknown_is_review():
    assert checker.verdict([checker.UNKNOWN, checker.UNKNOWN]) == "review"


def test_empty_sample_is_unknown():
    assert checker.verdict([]) == "unknown"


# --- closed comments --------------------------------------------------

def test_by_invitation_conversation_counts_as_closed():
    assert probe.conversation_is_open({"id_str": "1", "conversation_control": {"policy": "ByInvitation"}}) is False


def test_followers_only_conversation_counts_as_closed():
    assert probe.conversation_is_open({"id_str": "1", "conversation_control": {"policy": "Followers"}}) is False


def test_policy_all_is_open():
    assert probe.conversation_is_open({"id_str": "1", "conversation_control": {"policy": "All"}}) is True


def test_plain_post_is_open():
    assert probe.conversation_is_open({"id_str": "1"}) is True


def test_unavailable_tweet_is_closed():
    assert probe.conversation_is_open({"__typename": "TweetUnavailable"}) is False


def test_missing_id_is_closed():
    assert probe.conversation_is_open({"conversation_control": None}) is False


# --- window filtering -------------------------------------------------

def _seed(tmp_path, n=6):
    conn = connect(tmp_path)
    c = circle(key="eng_blue", topic_id=15, required=10)
    from engine import rounds as rounds_mod
    rid = rounds_mod.current_round(conn, c.key, ["06:00-18:00", "18:00-06:00"], dt(2026, 1, 1, 12)).id
    for i in range(n):
        ident, _ = idm.bind_handle(conn, 700 + i, f"dropper{i}", f"dp{i}_x")
        dbm.ex(
            conn,
            "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
            "account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("g", rid, c.key, i + 1, ident.id, ident.id, "l", f"tweet{i}", iso(dt(2026, 1, 1, 9, i))),
        )
    return conn, c


def test_closed_link_leaves_the_obligation_window(tmp_path):
    conn, c = _seed(tmp_path)
    member = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=705")["identity_id"]
    drop = lookup.latest_drop(conn, member)
    before = len(lookup.links_above(conn, drop, c))
    assert before > 0

    target = lookup.links_above(conn, drop, c)[0]
    checker.mark_comments_closed(conn, target["id"])

    after = lookup.links_above(conn, drop, c)
    assert target["id"] not in [r["id"] for r in after]
    assert len(after) == before - 1


def test_links_above_carries_owner_handle_for_urls(tmp_path):
    conn, c = _seed(tmp_path)
    member = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=705")["identity_id"]
    rows = lookup.links_above(conn, lookup.latest_drop(conn, member), c)
    assert all(r["owner_handle"] for r in rows)


# --- admin badge ------------------------------------------------------

def test_admin_drop_carries_a_badge(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 900, "boss", "boss_x")
    c = circle(key="eng_blue", topic_id=15, required=5)
    out = numbering.render_entry(c, 3, ident, "https://x.com/boss_x/status/1", is_admin=True)
    assert text_mod.ADMIN_BADGE in out
    assert "boss" in out


def test_member_drop_has_no_admin_badge(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 901, "member", "member_x")
    c = circle(key="eng_blue", topic_id=15, required=5)
    out = numbering.render_entry(c, 3, ident, "https://x.com/member_x/status/1", is_admin=False)
    assert text_mod.ADMIN_BADGE not in out


def test_admin_vip_shows_both_marks(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 902, "bossvip", "bossvip_x")
    conn.execute("UPDATE identities SET vip=1 WHERE id=?", (ident.id,))
    conn.commit()
    ident = idm.identity_by_id(conn, ident.id)
    c = circle(key="eng_blue", topic_id=15, required=5)
    out = numbering.render_entry(c, 3, ident, "https://x.com/bossvip_x/status/1", is_admin=True)
    assert text_mod.ADMIN_BADGE in out
    assert "VIP" in out


# --- reacted_on -------------------------------------------------------

def test_reacted_on_reads_the_reaction_table(tmp_path):
    conn, c = _seed(tmp_path)
    member = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=705")["identity_id"]
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, c)[0]
    assert checker.reacted_on(conn, row["id"], 705) is False
    dbm.ex(conn, "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
                 "VALUES (?,?,?,?)", (row["id"], 705, "❤", dbm.now_iso()))
    assert checker.reacted_on(conn, row["id"], 705) is True


def test_reacted_on_ignores_other_members(tmp_path):
    conn, c = _seed(tmp_path)
    member = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=705")["identity_id"]
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, c)[0]
    dbm.ex(conn, "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
                 "VALUES (?,?,?,?)", (row["id"], 999, "❤", dbm.now_iso()))
    assert checker.reacted_on(conn, row["id"], 705) is False


# --- confirmation before any punishment --------------------------------

class FakeReader:
    """Stands in for the browser reader: replies on the tweet ids in `replied_on`."""

    def __init__(self, replied_on=(), complete=True, open_tweets=()):
        self.replied_on = set(replied_on)
        self.complete = complete
        self.open_tweets = set(open_tweets)
        self.calls = []

    def __call__(self, tweet_id, owner_handle=None, member_handle=None):
        self.calls.append(tweet_id)
        return {
            "tweet_id": tweet_id,
            "url": f"https://x.com/{owner_handle}/status/{tweet_id}",
            "replied": tweet_id in self.replied_on if self.complete else None,
            "complete": self.complete,
            "open": tweet_id not in self.open_tweets,
        }


def _ident_id(conn, tg=705):
    return dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=?", (tg,))["identity_id"]


def _all_tweet_ids(conn, c):
    member = _ident_id(conn)
    return [r["tweet_id"] for r in lookup.links_above(conn, lookup.latest_drop(conn, member), c)]


def _react_all(conn, c, tg=705):
    member = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=705")["identity_id"]
    for row in lookup.links_above(conn, lookup.latest_drop(conn, member), c):
        dbm.ex(conn, "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, emoji, "
                     "observed_at) VALUES (?,?,?,?)", (row["id"], tg, "❤", dbm.now_iso()))


def test_clear_sample_needs_no_confirmation(tmp_path):
    conn, c = _seed(tmp_path)
    _react_all(conn, c)
    ids = _all_tweet_ids(conn, c)
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0),
                                       FakeReader(replied_on=ids))
    assert result["verdict"] == "clear"
    assert result["confirmed"] is False
    assert result["checked"] == 2


def test_violated_only_after_a_second_failing_sample(tmp_path):
    conn, c = _seed(tmp_path)
    ids = _all_tweet_ids(conn, c)
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0),
                                       FakeReader(replied_on=()))
    assert result["verdict"] == "violated"
    assert result["confirmed"] is True
    assert result["checked"] == 4, "both samples must be read"


def test_second_sample_clearing_downgrades_to_review(tmp_path):
    """The whole point: one bad sample must not punish an honest member."""
    conn, c = _seed(tmp_path)
    _react_all(conn, c)
    ids = _all_tweet_ids(conn, c)

    class SecondSampleWins(FakeReader):
        def __call__(self, tweet_id, owner_handle=None, member_handle=None):
            out = super().__call__(tweet_id, owner_handle, member_handle)
            out["replied"] = tweet_id in ids if len(self.calls) >= 2 else False
            self.calls.append(tweet_id)
            return out

    reader = SecondSampleWins()
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0), reader)
    assert result["verdict"] == "review"
    assert result["confirmed"] is False


def test_unreadable_first_sample_goes_straight_to_review(tmp_path):
    conn, c = _seed(tmp_path)
    _react_all(conn, c)
    reader = FakeReader(complete=False)
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0), reader)
    assert result["verdict"] == "review"
    assert len(reader.calls) == 2, "no second sample when the first is unreadable"


def test_missing_reaction_is_a_miss_even_when_x_is_unreadable(tmp_path):
    """Reactions are proven by Telegram alone, so an unread thread does not excuse them."""
    conn, c = _seed(tmp_path)
    reader = FakeReader(complete=False)
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0), reader,
                                       confirm=False)
    assert result["verdict"] == "violated"


def test_closed_comment_cannot_confirm_a_defaulter(tmp_path):
    conn, c = _seed(tmp_path)
    ids = _all_tweet_ids(conn, c)
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0),
                                       FakeReader(replied_on=(), open_tweets=set(ids)))
    assert result["verdict"] == "review"
    assert result["confirmed"] is False


def test_confirmation_can_be_switched_off(tmp_path):
    conn, c = _seed(tmp_path)
    reader = FakeReader(replied_on=())
    result = sampling.evaluate_sampled(conn, _ident_id(conn), c, 2, random.Random(0), reader,
                                       confirm=False)
    assert result["verdict"] == "violated"
    assert len(reader.calls) == 2


# --- the one allowed emoji --------------------------------------------

def test_heart_with_and_without_variation_selector_are_equal():
    assert checker.is_allowed_emoji("❤")
    assert checker.is_allowed_emoji("❤️")
    assert checker.is_allowed_emoji("\u2764\ufe0f")


def test_other_emojis_are_not_engagement():
    for emoji in ("👍", "🔥", "😡", "❤❤", "❤️🔥", "💔", ""):
        assert checker.is_allowed_emoji(emoji) is False


def test_reaction_row_with_wrong_emoji_does_not_count(tmp_path):
    conn, c = _seed(tmp_path)
    member = _ident_id(conn)
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, c)[0]
    dbm.ex(conn, "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
                 "VALUES (?,?,?,?)", (row["id"], 705, "🔥", dbm.now_iso()))
    assert checker.reacted_on(conn, row["id"], 705) is False


def test_reaction_row_with_heart_counts(tmp_path):
    conn, c = _seed(tmp_path)
    member = _ident_id(conn)
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, c)[0]
    dbm.ex(conn, "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
                 "VALUES (?,?,?,?)", (row["id"], 705, "❤️", dbm.now_iso()))
    assert checker.reacted_on(conn, row["id"], 705) is True


def test_normalize_strips_noise():
    assert checker.normalize_emoji("\u2764\ufe0f ") == "\u2764"
    assert checker.normalize_emoji(None) == ""


# --- likes circle is reaction only ------------------------------------

class _LikeCircle:
    key = "eng_blue"
    topic_id = 16
    kind = "likes"
    required = 10
    entry_format = "slash"


def test_likes_circle_never_opens_x(tmp_path):
    conn, _ = _seed(tmp_path)
    reader = FakeReader(replied_on=())
    result = sampling.evaluate_sampled(conn, _ident_id(conn), _LikeCircle(), 2,
                                       random.Random(0), reader)
    assert reader.calls == [], "a likes circle must not read X at all"
    assert result["verdict"] == "violated"
    assert result["confirmed"] is True


def test_likes_circle_passes_on_hearts_alone(tmp_path):
    conn, _ = _seed(tmp_path)
    _react_all(conn, _LikeCircle())
    reader = FakeReader(replied_on=())
    result = sampling.evaluate_sampled(conn, _ident_id(conn), _LikeCircle(), 2,
                                       random.Random(0), reader)
    assert reader.calls == []
    assert result["verdict"] == "clear"


def test_likes_circle_still_needs_the_allowed_emoji(tmp_path):
    conn, _ = _seed(tmp_path)
    member = _ident_id(conn)
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, _LikeCircle())[0]
    dbm.ex(conn, "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) "
                 "VALUES (?,?,?,?)", (row["id"], 705, "👍", dbm.now_iso()))
    assert checker.link_state(False, None, True, True, need_comment=False) == checker.MISS


def test_link_state_ignores_replied_for_reaction_only_circle():
    assert checker.link_state(True, False, True, True, need_comment=False) == checker.ENGAGED
    assert checker.link_state(True, None, True, False, need_comment=False) == checker.ENGAGED

# --- divider helper matches the circle ---------------------------------

def test_likes_circle_helper_asks_for_a_like_not_a_comment():
    assert text_mod.helper_for("likes") == text_mod.HELPER_LIKE
    assert "comment" not in text_mod.helper_for("likes")
    assert "like" in text_mod.helper_for("likes")


def test_engagement_circle_helper_asks_for_a_comment():
    assert text_mod.helper_for("engagement") == text_mod.HELPER_COMMENT
    assert "comment" in text_mod.helper_for("engagement")


def test_repost_circle_uses_the_comment_helper():
    assert text_mod.helper_for("repost") == text_mod.HELPER_COMMENT


def test_both_helpers_ask_for_the_heart():
    for kind in ("likes", "engagement", "repost"):
        assert "\u2764" in text_mod.helper_for(kind)


# --- reaction updates mirror the real state ---------------------------

def _drop_with_entry(conn, c, tg=705):
    member = _ident_id(conn, tg)
    drop = lookup.latest_drop(conn, member)
    row = lookup.links_above(conn, drop, c)[0]
    return row


def test_reaction_stored_from_a_raw_update_like_the_event_itself(tmp_path):
    """events.Raw hands over the update object itself, not event.update.

    Reading event.update raised AttributeError on every reaction, so nothing was ever
    recorded and no member could ever look engaged.
    """
    import asyncio
    from types import SimpleNamespace
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    row = _drop_with_entry(conn, c)
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (4242, row["id"]))

    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = conn

    update = SimpleNamespace(
        msg_id=4242,
        actor=SimpleNamespace(user_id=705),
        new_reactions=[SimpleNamespace(emoticon="\u2764\ufe0f", document_id=None)],
    )
    asyncio.run(bot.on_reaction(update))
    assert checker.reacted_on(conn, row["id"], 705) is True


def test_reaction_from_a_wrapped_event_also_works(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    row = _drop_with_entry(conn, c)
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (4343, row["id"]))

    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = conn

    wrapped = SimpleNamespace(update=SimpleNamespace(
        msg_id=4343,
        actor=SimpleNamespace(user_id=705),
        new_reactions=[SimpleNamespace(emoticon="\u2764", document_id=None)],
    ))
    asyncio.run(bot.on_reaction(wrapped))
    assert checker.reacted_on(conn, row["id"], 705) is True


def test_empty_update_is_ignored(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = conn
    asyncio.run(bot.on_reaction(SimpleNamespace()))


def test_only_the_allowed_emoji_is_stored(tmp_path):
    from datetime import datetime, timezone
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    row = _drop_with_entry(conn, c)
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (777, row["id"]))
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    qb.store_reaction(conn, 777, 705, "👍", now)
    assert checker.reacted_on(conn, row["id"], 705) is False

    qb.store_reaction(conn, 777, 705, "❤️", now)
    assert checker.reacted_on(conn, row["id"], 705) is True


def test_switching_away_from_the_heart_withdraws_the_credit(tmp_path):
    from datetime import datetime, timezone
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    row = _drop_with_entry(conn, c)
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (888, row["id"]))
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    qb.store_reaction(conn, 888, 705, "❤", now)
    assert checker.reacted_on(conn, row["id"], 705) is True
    qb.store_reaction(conn, 888, 705, "🔥", now)
    assert checker.reacted_on(conn, row["id"], 705) is False
    qb.store_reaction(conn, 888, 705, "", now)
    assert checker.reacted_on(conn, row["id"], 705) is False


# --- reaction sync from the userbot -----------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, boom=False):
        self.status_code = status_code
        self._payload = payload
        self._boom = boom

    def json(self):
        if self._boom:
            raise ValueError("not json")
        return self._payload


class _FakeHttp:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def get(self, *a, **kw):
        if self._error:
            raise self._error
        return self._response


def test_probe_fails_closed_on_transport_error():
    from engine.x_reader import probe

    http = _FakeHttp(error=RuntimeError("dns down"))
    assert probe.probe_comments_open("123", client=http) is False


def test_probe_fails_closed_on_bad_status():
    from engine.x_reader import probe

    http = _FakeHttp(response=_FakeResponse(status_code=500))
    assert probe.probe_comments_open("123", client=http) is False


def test_probe_fails_closed_on_unreadable_body():
    from engine.x_reader import probe

    http = _FakeHttp(response=_FakeResponse(boom=True))
    assert probe.probe_comments_open("123", client=http) is False


def test_probe_reports_open_when_confirmed():
    from engine.x_reader import probe

    http = _FakeHttp(response=_FakeResponse(payload={"id_str": "123"}))
    assert probe.probe_comments_open("123", client=http) is True


# --- putting a member back to normal ----------------------------------

def _member(conn, tg=7900, name="resetme", handle="resetmex"):
    ident, _ = idm.bind_handle(conn, tg, name, handle)
    drop_id = dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, "
                           "identity_id, account_id, link, tweet_id, created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?)",
                     ("g", 1, "eng_blue", 1, ident.id, ident.id, "l", "t1", dbm.now_iso()))
    return ident, drop_id


def test_reset_clears_debt_cases_and_queued_punishment(tmp_path):
    conn = connect(tmp_path)
    ident, drop_id = _member(conn)

    dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                 "VALUES (?,?,?,?)", (ident.id, drop_id, 30, dbm.now_iso()))
    dbm.ex(conn, "INSERT INTO cases (identity_id, drop_id, missing_count, status, opened_at) "
                 "VALUES (?,?,?,?,?)", (ident.id, drop_id, 2, "open", dbm.now_iso()))
    dbm.ex(conn, "INSERT INTO punish_intents (identity_id, case_id, kind, mode, status, created_at) "
                 "VALUES (?,?,?,?,?,?)",
           (ident.id, None, "warn", "list_only", "queued", dbm.now_iso()))

    out = idm.reset_member(conn, ident.id, closed_by=1)
    assert out["obligations"] == 1
    assert out["cases"] == 1
    assert out["punish_intents_cancelled"] == 1

    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations WHERE cleared_at IS NULL")["n"] == 0
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases WHERE status='open'")["n"] == 0
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punish_intents WHERE status='queued'")["n"] == 0
    row = dbm.q1(conn, "SELECT * FROM cases WHERE identity_id=?", (ident.id,))
    assert row["close_reason"] == "admin_reset"


def test_reset_touches_only_that_member(tmp_path):
    conn = connect(tmp_path)
    a, da = _member(conn, 7900, "alpha", "alphax")
    b, db_ = _member(conn, 7901, "beta", "betax")
    for ident, drop in ((a, da), (b, db_)):
        dbm.ex(conn, "INSERT INTO obligations (identity_id, drop_id, required, created_at) "
                     "VALUES (?,?,?,?)", (ident.id, drop, 30, dbm.now_iso()))

    idm.reset_member(conn, a.id)
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations "
                        "WHERE cleared_at IS NULL AND identity_id=?",
                  (a.id,))["n"] == 0
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations "
                        "WHERE cleared_at IS NULL AND identity_id=?",
                  (b.id,))["n"] == 1


def test_cleared_command_message_reports_cases_and_intents():
    from engine import text

    msg = text.CLEARED.format(n=2, tg="budi", at="12:00") + text.CLEARED_DETAIL.format(
        cases=1, intents=0)
    assert "2 obligation(s) of @budi" in msg
    assert "Closed 1 open case(s)" in msg
    assert "Back to normal" in msg


# --- a user session cannot see who reacted, the bot can ---------------

def _with_users(emojis, users, recent=None):
    """Shape the bot receives: MessageReactionsWithUsers."""
    from types import SimpleNamespace

    return SimpleNamespace(reactions=SimpleNamespace(
        results=[SimpleNamespace(reaction=SimpleNamespace(emoticon=e), count=1)
                 for e in emojis],
        users=[SimpleNamespace(id=u) for u in users],
        recent_reactions=recent or [],
    ))


def test_reaction_emoji_reads_emoticon():
    """new_reactions carries ReactionEmoji, which has .emoticon, not .emoji.

    Reading .emoji always returned empty, so every heart looked like "not a heart" and
    no member was ever credited.
    """
    from types import SimpleNamespace
    from services.queue_bot import _reaction_emoji

    assert _reaction_emoji(SimpleNamespace(emoticon="\u2764", document_id=None)) == "\u2764"


def test_reaction_emoji_handles_custom_and_wrapped():
    from types import SimpleNamespace
    from services.queue_bot import _reaction_emoji

    assert _reaction_emoji(SimpleNamespace(emoticon=None, document_id=777)) == "custom:777"
    wrapped = SimpleNamespace(reaction=SimpleNamespace(emoticon="\u2764"))
    assert _reaction_emoji(wrapped) == "\u2764"
    assert _reaction_emoji(SimpleNamespace()) == ""


def test_heart_from_the_wire_actually_counts(tmp_path):
    """End to end: a real wire reaction object has to register as engagement."""
    import asyncio
    from types import SimpleNamespace
    from services import queue_bot as qb

    conn, c = _seed(tmp_path)
    row = _drop_with_entry(conn, c)
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (5150, row["id"]))

    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = conn

    update = SimpleNamespace(
        msg_id=5150,
        actor=SimpleNamespace(user_id=705),
        new_reactions=[SimpleNamespace(emoticon="\u2764", document_id=None)],
    )
    asyncio.run(bot.on_reaction(update))
    assert checker.reacted_on(conn, row["id"], 705) is True

    update.new_reactions = [SimpleNamespace(emoticon=None, document_id=999)]
    asyncio.run(bot.on_reaction(update))
    assert checker.reacted_on(conn, row["id"], 705) is False


# --- downtime must never be punished on the member --------------------

def test_reaction_gap_is_zero_without_a_heartbeat(tmp_path):
    conn, c = _seed(tmp_path)
    assert checker.reaction_gap(conn, dbm.now_iso()) == 0


def test_reaction_gap_is_zero_without_a_recorded_outage(tmp_path):
    conn, c = _seed(tmp_path)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("hb:queue_bot", "2030-01-01T00:00:00+00:00"))
    assert checker.reaction_gap(conn, "2026-01-01T09:00:00+00:00") == 0


def test_reaction_gap_ignores_a_malformed_timestamp(tmp_path):
    conn, c = _seed(tmp_path)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("down:from", "not-a-date"))
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("down:to", "also-not-a-date"))
    assert checker.reaction_gap(conn, "2026-01-01T09:00:00+00:00") == 0


def test_downtime_downgrades_a_violation_to_review(tmp_path):
    """Our own restart must not turn an honest member into a defaulter."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    conn, c = _seed(tmp_path)
    member = _ident_id(conn)
    drop = lookup.latest_drop(conn, member)
    created = datetime.fromisoformat(drop["created_at"])
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("down:from", created.isoformat()))
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("down:to", (created + timedelta(hours=3)).isoformat()))

    class NoReactions:
        def __call__(self, tweet_id, owner_handle=None, member_handle=None):
            return {"tweet_id": tweet_id, "url": None, "replied": False,
                    "complete": True, "open": True}

    res = sampling.evaluate_sampled(conn, member, c, 2, random.Random(0), NoReactions())
    assert res["verdict"] == "review"
    assert res.get("downtime_seconds", 0) > 0
    assert sampling.open_case_for(conn, res, SimpleNamespace(miss_threshold=2), member) is None


# --- commands must not litter a circle topic --------------------------

class _CmdMsg:
    """A command message. topic_id_of reads .reply_to, so the fake must carry it."""

    def __init__(self, topic, text="/cleared someone"):
        self.text = text
        self.reply_to_msg_id = topic
        self.reply_to = None
        if topic is not None:
            self.reply_to = type("R", (), {"forum_topic": True})()
        self.chat_id = -100
        self.id = 4242
        self.sender_id = 9001
        self.sender = type("S", (), {"username": "boss", "first_name": "boss"})

    async def reply(self, body, **kw):
        raise AssertionError("a command in a circle must not be answered in place")


def test_command_in_a_circle_is_deleted_and_answered_in_prison(tmp_path):
    import asyncio
    from services import queue_bot as qb

    conn = connect(tmp_path)
    sent = []

    class FakeClient:
        async def delete_messages(self, chat_id, ids):
            sent.append(("deleted", ids))

        async def send_message(self, chat_id, text, reply_to=None, buttons=None,
                               parse_mode=None, link_preview=True):
            sent.append(("sent", reply_to, text))
            return type("M", (), {"id": 1})()

    group = type("G", (), {"main_group": -100, "topics": {"prison": 17},
                           "circle_by_topic": staticmethod(lambda t: 15 if t == 15 else None)})()
    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = conn
    bot.client = FakeClient()

    asyncio.run(bot.reply_cmd(group, _CmdMsg(15), "cleared"))
    assert sent[0][0] == "deleted", sent
    assert sent[1][1] == 17, f"answer must land in prison, got {sent[1]}"


def test_command_in_prison_is_left_where_it_belongs(tmp_path):
    """Only junk in the wrong place is removed. An admin running a command in the
    prison topic must keep their own record of it."""
    import asyncio
    from services import queue_bot as qb

    deleted, replied = [], []

    class FakeClient:
        async def delete_messages(self, chat_id, ids):
            deleted.append(ids)

        async def send_message(self, chat_id, text, reply_to=None, buttons=None,
                               parse_mode=None, link_preview=True):
            replied.append(("topic", reply_to, text))
            return type("M", (), {"id": 9})()

    class Msg:
        text = "/cleared someone"
        reply_to_msg_id = 17          # the prison topic
        chat_id = -100
        id = 2
        sender_id = 9001
        sender = type("S", (), {"username": "boss", "first_name": "boss"})
        reply_to = type("R", (), {"forum_topic": True})()

        async def reply(self, body, **kw):
            replied.append(("inplace", None, body))

    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = connect(tmp_path)
    bot.client = FakeClient()
    group = type("G", (), {"main_group": -100, "topics": {"prison": 17},
                           "circle_by_topic": staticmethod(lambda t: 15 if t == 15 else None)})()
    asyncio.run(bot.reply_cmd(group, Msg(), "ok"))
    assert deleted == [], "a command typed in prison is not junk"
    assert replied[0][0] == "inplace"


def test_command_in_a_circle_is_deleted_and_answered_in_prison(tmp_path):
    """Only junk in the wrong place is removed."""
    import asyncio
    from services import queue_bot as qb

    deleted, sent = [], []

    class FakeClient:
        async def delete_messages(self, chat_id, ids):
            deleted.append(ids)

        async def send_message(self, chat_id, text, reply_to=None, buttons=None,
                               parse_mode=None, link_preview=True):
            sent.append((reply_to, text))
            return type("M", (), {"id": 9})()

    class Plain:
        text = "/cleared someone"
        reply_to_msg_id = 15          # typed inside a circle
        chat_id = -100
        id = 1
        sender_id = 9001
        sender = type("S", (), {"username": "boss", "first_name": "boss"})
        reply_to = type("R", (), {"forum_topic": True})()

        async def reply(self, body, **kw):
            raise AssertionError("must not answer in place")

    bot = qb.QueueBot.__new__(qb.QueueBot)
    bot.conn = connect(tmp_path)
    bot.client = FakeClient()
    group = type("G", (), {"main_group": -100, "topics": {"prison": 17},
                           "circle_by_topic": staticmethod(lambda t: 15 if t == 15 else None)})()
    asyncio.run(bot.reply_cmd(group, Plain(), "ok"))
    assert deleted == [1]
    assert sent[0][0] == 17


# --- downtime is recorded as a window, not measured against a heartbeat

def test_no_previous_heartbeat_means_no_downtime_recorded(tmp_path):
    conn = connect(tmp_path)
    assert checker.record_startup_downtime(conn, grace_seconds=60) == 0
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM kv WHERE key='down:from'")["n"] == 0


def test_a_fresh_heartbeat_is_not_downtime(tmp_path):
    from datetime import datetime, timezone

    conn = connect(tmp_path)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("hb:queue_bot", datetime.now(timezone.utc).isoformat()))
    assert checker.record_startup_downtime(conn, grace_seconds=60) == 0


def test_stale_heartbeat_records_a_downtime_window(tmp_path):
    from datetime import datetime, timedelta, timezone

    conn = connect(tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("hb:queue_bot", stale.isoformat()))
    gap = checker.record_startup_downtime(conn, grace_seconds=60)
    assert gap > 6000
    assert dbm.q1(conn, "SELECT value FROM kv WHERE key='down:from'")["value"]


def test_gap_is_zero_for_a_drop_created_after_the_outage(tmp_path):
    """Every drop is older than a live heartbeat, so measuring against one would
    downgrade every single check and nothing could ever be punished."""
    from datetime import datetime, timedelta, timezone

    conn = connect(tmp_path)
    now = datetime.now(timezone.utc)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("hb:queue_bot", now.isoformat()))
    assert checker.record_startup_downtime(conn, grace_seconds=60) == 0

    old_drop = (now - timedelta(days=1)).isoformat()
    new_drop = (now - timedelta(minutes=1)).isoformat()
    assert checker.reaction_gap(conn, old_drop) == 0
    assert checker.reaction_gap(conn, new_drop) == 0


def test_old_drop_during_an_outage_is_covered(tmp_path):
    from datetime import datetime, timedelta, timezone

    conn = connect(tmp_path)
    now = datetime.now(timezone.utc)
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
           ("hb:queue_bot", (now - timedelta(hours=2)).isoformat()))
    checker.record_startup_downtime(conn, grace_seconds=60)
    during = (now - timedelta(hours=1)).isoformat()
    assert checker.reaction_gap(conn, during) > 0


def test_no_outage_means_a_miss_is_a_real_miss(tmp_path):
    """With no downtime recorded, a missing heart is a genuine miss."""
    conn, c = _seed(tmp_path)
    assert checker.reaction_gap(conn, dbm.now_iso()) == 0


# --- the checker must not advertise how it samples ---------------------

def test_acknowledgement_never_mentions_sampling():
    """Telling members the sample is random invites them to game it."""
    from engine import text

    started = text.CHECK_STARTED.format(name="budi")
    lowered = started.lower()
    for leak in ("random", "sample", "2 ", "link"):
        assert leak not in lowered, f"{leak!r} leaks how the check works: {started!r}"


def test_check_is_admin_only():
    """A member who can check themselves learns when they are being watched."""
    from pathlib import Path

    src = Path("services/queue_bot.py").read_text()
    body = src[src.index("async def cmd_check"):]
    body = body[:body.index("async def run_check")]
    assert "if not caller_admin:" in body
    assert "text.ADMIN_ONLY" in body
    assert "CHECK_RESULT_MEMBER" not in body


def test_usage_says_admins_only():
    from engine import text

    assert "Admins only" in text.USAGE_CHECK


def test_verdict_words_do_not_reveal_counts():
    from engine import text

    for key, word in text.CHECK_VERDICT_WORDS.items():
        assert not any(ch.isdigit() for ch in word), (key, word)


def test_admin_result_still_carries_the_detail():
    from engine import text

    out = text.CHECK_RESULT_ADMIN.format(name="budi", verdict="REVIEW",
                                         body="Drop #5\nOK react=yes reply=yes 123")
    assert "REVIEW" in out and "123" in out


def test_reset_reports_the_real_count_not_a_rowid(tmp_path):
    """dbm.ex returns lastrowid, which once made /cleared claim 6147 cancellations
    when only 2 were cancelled."""
    conn = connect(tmp_path)
    ident, _drop_id = _member(conn, 7950, "counted", "countedx")
    for _ in range(3):
        dbm.ex(conn, "INSERT INTO punish_intents (identity_id, case_id, kind, mode, "
                     "status, created_at) VALUES (?,?,?,?,?,?)",
               (ident.id, None, "warn", "list_only", "queued", dbm.now_iso()))

    # 'queued' is the status the punishment worker reads; filtering on 'pending' would
    # silently cancel nothing.
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punish_intents WHERE status='queued'")["n"] == 3
    out = idm.reset_member(conn, ident.id)
    assert out["punish_intents_cancelled"] == 3, out
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punish_intents WHERE status='cancelled'")["n"] == 3


def test_admin_drops_never_create_an_obligation(tmp_path):
    """Admins are never judged, so they must never take on debt either."""
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7960, "boss", "bossx")
    account = SimpleNamespace(id=dbm.q1(
        conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7960")["identity_id"])
    circle = SimpleNamespace(key="eng_blue", topic_id=15, required=30, entry_format="slash")

    counter = {"n": 0}

    def make_intent(is_admin):
        counter["n"] += 1
        return {"group_key": "g", "round_id": 1, "identity": ident,
                "account": account, "position": counter["n"], "is_admin": is_admin}

    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    qb.commit_drop(conn, circle, make_intent(True), "bossx", "999", stamp)
    qb.commit_drop(conn, circle, make_intent(False), "bossx", "1000", stamp)

    open_for_admin = dbm.q1(
        conn, "SELECT COUNT(*) n FROM obligations WHERE identity_id=? AND cleared_at IS NULL",
        (ident.id,))["n"]
    assert open_for_admin == 1, "exactly one, and it must belong to the non-admin drop"


# --- every reply_cmd call site must match its signature ----------------

def pathlib_source(name):
    from pathlib import Path
    return Path(name).read_text()


def test_no_caller_passes_arguments_reply_cmd_does_not_accept():
    """Changing msg.reply to reply_cmd left a stray link_preview argument, and the
    check result crashed on delivery after the browser had already done its work."""
    import ast
    import inspect
    from services import queue_bot as qb

    params = set(inspect.signature(qb.QueueBot.reply_cmd).parameters)
    tree = ast.parse(pathlib_source("services/queue_bot.py"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "reply_cmd"):
            continue
        kws = {k.arg for k in node.keywords if k.arg}
        extra = kws - params
        if extra:
            bad.append((node.lineno, sorted(extra)))
    assert not bad, f"reply_cmd called with unsupported arguments: {bad}"


def test_reply_cmd_signature_is_stable():
    import inspect
    from services import queue_bot as qb

    params = set(inspect.signature(qb.QueueBot.reply_cmd).parameters)
    assert params == {"self", "group", "msg", "body", "buttons", "parse_mode"}, params


def test_check_result_buttons_are_real_button_objects():
    """The card buttons were still plain dicts, so Telegram rejected the whole reply
    after the browser had already finished reading."""
    from services import queue_bot as qb
    from engine import sampling

    result = {
        "verdict": "review", "checked": 2, "window_total": 30, "links": [
            {"tweet_id": "1234567890", "state": "miss", "liked": False, "replied": False,
             "url": "https://x.com/someone/status/1234567890"},
        ],
    }
    _body, buttons = sampling.build_card(result)
    assert buttons and isinstance(buttons[0][0], dict)

    real = qb.QueueBot.build_buttons(buttons)
    assert type(real[0][0]).__name__ == "KeyboardInlineButton"


# --- /del and /label must agree, and /del must keep its reason --------

def test_delete_returns_the_reason_it_just_stored(tmp_path):
    """The note used to say "Reason: None" because the row was read before the update."""
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7970, "wrong", "wrongx")
    dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
                 "account_id, link, tweet_id, status, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
           ("g", 1, "eng_blue", 7, ident.id, ident.id, "l", "t7", "active", dbm.now_iso()))

    out = qb.delete_wrong_link(conn, "eng_blue", 7, 9001, "salah orang")
    assert out["drop"]["deleted_reason"] == "salah orang", out["drop"]["deleted_reason"]
    assert out["drop"]["status"] == "deleted_wrong_link"
    assert out["drop"]["deleted_by"] == 9001


def test_delete_without_a_reason_still_records_something(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7971, "noreason", "noreasonx")
    dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
                 "account_id, link, tweet_id, status, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
           ("g", 1, "eng_blue", 8, ident.id, ident.id, "l", "t8", "active", dbm.now_iso()))
    out = qb.delete_wrong_link(conn, "eng_blue", 8, 9001, None)
    assert out["drop"]["deleted_reason"] == "unspecified"


def test_delete_is_blocked_while_a_case_is_open(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, drop_id = _member(conn, 7972, "busy", "busyx")
    dbm.ex(conn, "UPDATE drops SET circle_key='eng_blue' WHERE id=?", (drop_id,))
    dbm.ex(conn, "INSERT INTO cases (identity_id, drop_id, missing_count, status, opened_at) "
                 "VALUES (?,?,?,?,?)", (ident.id, drop_id, 2, "open", dbm.now_iso()))
    out = qb.delete_wrong_link(conn, "eng_blue", 1, 9001, "x")
    assert out["blocked"] == "open_case"


def test_label_usage_explains_both_forms():
    from engine import text

    assert "circle_key" in text.USAGE_LABEL
    assert "/label <position>" in text.USAGE_LABEL


def test_label_works_on_a_live_entry(tmp_path):
    """/label looked for status='unlabeled', a value nothing ever wrote, so it could
    never succeed on any real drop."""
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7980, "target", "targetx")
    dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, "
                 "account_id, link, tweet_id, status, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
           ("g", 1, "eng_blue", 3, ident.id, ident.id, "l", "t3", "active", dbm.now_iso()))

    out = qb.label_drop(conn, "eng_blue", 3, "wrong")
    assert out is not None
    row = dbm.q1(conn, "SELECT status FROM drops WHERE position=3")
    assert row["status"] == "deleted_wrong"


def test_label_rejects_an_unknown_label(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    assert qb.label_drop(conn, "eng_blue", 3, "banana") is None


# --- a failed send must not cost the member their number ---------------

def test_rollback_frees_the_number_and_the_debt(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7990, "rolled", "rolledx")
    dbm.ex(conn, "INSERT INTO drop_credits (identity_id, round_id, granted_for, reason, "
                 "created_at) VALUES (?,?,?,?,?)", (ident.id, 1, "wrong", "r", dbm.now_iso()))
    circle = SimpleNamespace(key="eng_blue", topic_id=15, required=30, entry_format="three")

    intent = {"group_key": "g", "round_id": 1, "identity": ident,
              "account": SimpleNamespace(id=ident.id), "position": 1, "is_admin": False}
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    committed = qb.commit_drop(conn, circle, intent, "rolledx", "999", stamp)
    assert committed["obligation_id"]
    assert committed["credit_id"]

    qb.rollback_drop(conn, committed)

    assert dbm.q1(conn, "SELECT COUNT(*) n FROM drops")["n"] == 0, "drop row left behind"
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM obligations")["n"] == 0, "debt left behind"
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM drop_credits "
                        "WHERE consumed_at IS NOT NULL")["n"] == 0, "credit consumed for nothing"
    # the number is free again, so the member's retry gets the same one
    assert numbering.next_position(conn, "eng_blue") == 1


def test_rollback_is_safe_when_nothing_was_committed(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    qb.rollback_drop(conn, {})
    qb.rollback_drop(conn, None)
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM drops")["n"] == 0


def test_commit_drop_reports_what_to_undo(tmp_path):
    from services import queue_bot as qb

    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 7991, "kept", "keptx")
    circle = SimpleNamespace(key="eng_blue", topic_id=15, required=30, entry_format="three")
    intent = {"group_key": "g", "round_id": 1, "identity": ident,
              "account": SimpleNamespace(id=ident.id), "position": 1, "is_admin": False}
    out = qb.commit_drop(conn, circle, intent, "keptx", "1",
                         datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert out["drop_id"]
    assert out["obligation_id"]
    assert out["credit_id"] is None, "no credit should have been consumed"


# --- a case must reach the punishment queue, not stop at the case -------

def test_open_case_reaches_the_punishment_queue(tmp_path):
    """The whole chain: /check opens a case, the scheduler escalates it past the
    deadline, and a punish intent lands in the queue for the userbot."""
    from engine import config as cfgmod
    from engine import escalation
    from engine import identity as idm
    from engine import rounds as rounds_mod
    from engine import sampling
    from services import scheduler as sched

    SCHEDULE = ["06:00-18:00", "18:00-06:00"]
    conn = connect(tmp_path)
    c = circle(key="eng_blue", topic_id=15, required=10)
    rid = rounds_mod.current_round(conn, c.key, SCHEDULE, dt(2026, 1, 1, 12)).id
    # enough links above so the confirming sample still has somewhere to draw from
    for i in range(8):
        ident, _ = idm.bind_handle(conn, 7100 + i, f"esc{i}", f"esc{i}_x")
        dbm.ex(conn, "INSERT INTO drops (group_key, round_id, circle_key, position, "
                     "identity_id, account_id, link, tweet_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
               ("hotti", rid, c.key, i + 1, ident.id, ident.id, "l", f"t{i}",
                iso(dt(2026, 1, 1, 9, i))))
    victim = dbm.q1(conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7107")["identity_id"]

    rules = SimpleNamespace(enforce_deadline_minutes=15, max_warnings=3,
                            warn_ladder={1: "4h", 2: "1d", 3: "7d"})
    res = sampling.evaluate_sampled(conn, victim, c, 2, random.Random(0), _SaidNothing())
    assert res["verdict"] == "violated", res["verdict"]
    case_id = sampling.open_case_for(conn, res, rules, victim)
    assert case_id
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM cases WHERE status='open'")["n"] == 1

    # open_case stamps its own opened_at with the real clock, so "now" for the deadline
    # check has to sit past that, not on a fixed date in the past.
    group = SimpleNamespace(key="hotti", main_group=-1, topics={"prison": 17})
    deadline_now = datetime.now(timezone.utc) + timedelta(days=1)
    due = escalation.cases_past_deadline(conn, "hotti", rules, deadline_now)
    assert any(r["id"] == case_id for r in due), "the case is never past its deadline"

    moved = []
    bot = sched.Scheduler.__new__(sched.Scheduler)
    bot.conn = conn
    bot.cfg = SimpleNamespace(rules=rules, enforcement=SimpleNamespace(mode="list_only"))
    result = escalation.escalate_case(conn, group, rules, due[0], "list_only", deadline_now)
    assert result is not None
    assert dbm.q1(conn, "SELECT COUNT(*) n FROM punishments")["n"] == 1


class _SaidNothing:
    def __call__(self, tweet_id, owner_handle=None, member_handle=None):
        return {"tweet_id": tweet_id, "url": None, "replied": False,
                "complete": True, "open": True}
