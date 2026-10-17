"""Decide whether a member really failed to engage.

This is the part that can hurt a real person, so the rules are deliberately strict:

  clear     every sampled link was engaged
  violated  every sampled link was missed, and every miss is proven
  review    anything unproven: unread thread, closed comments, mixed sample

A missing comment counts as a defaulter only when the thread was read to the very end.
A partial read is never a miss, it is a question for a human. A post with closed
comments is never judgeable at all, because nobody could have commented on it.

Any single unknown on any sampled link forces `review`, so a partial read can never
reach an automatic punishment.
"""

import logging
from datetime import datetime, timezone

from engine import db as dbm

log = logging.getLogger("checker")

ENGAGED = "engaged"
MISS = "miss"
UNKNOWN = "unknown"

# A member proves they engaged by putting the heart on the numbered entry above their
# drop. Exactly one emoji is accepted: U+2764. Any other reaction is ignored rather
# than counted, so nothing can quietly stand in for real engagement.
ALLOWED_EMOJI = "\u2764"
_EMOJI_NOISE = ("\ufe0f", "\ufe0e", " ", "\u200d", "\n", "\t")


def normalize_emoji(emoji) -> str:
    """Telegram sends the heart with and without a variation selector; treat them equal."""
    if not emoji:
        return ""
    out = emoji
    for junk in _EMOJI_NOISE:
        out = out.replace(junk, "")
    return out.strip()


def is_allowed_emoji(emoji) -> bool:
    return normalize_emoji(emoji) == ALLOWED_EMOJI

# Circles of kind "likes" are reaction-only. Nothing is read from X there.
NEEDS_COMMENT_KINDS = frozenset({"engagement", "repost"})


def _field(row, name):
    """Read a column from a sqlite3.Row or a plain dict."""
    if row is None:
        return None
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def link_state(reacted, replied, comments_open=True, read_complete=True,
               need_comment=True) -> str:
    """Judge one sampled link from what we actually know about it.

    In a comments circle the member owes both a heart and a reply. In a likes circle
    only the heart is asked for, so X is never consulted.
    """
    if not need_comment:
        return ENGAGED if reacted else MISS
    if comments_open is False:
        return UNKNOWN
    if reacted is False:
        # Telegram reactions are authoritative on their own, no X read needed.
        return MISS
    if replied is None or not read_complete:
        return UNKNOWN
    return ENGAGED if replied else MISS


def verdict(states) -> str:
    states = list(states)
    if not states:
        return "unknown"
    if any(s == UNKNOWN for s in states):
        return "review"
    if all(s == ENGAGED for s in states):
        return "clear"
    if all(s == MISS for s in states):
        return "violated"
    return "review"


def record_startup_downtime(conn, grace_seconds: int = 60) -> int:
    """Work out how long the bot was down before it came back, and remember it.

    Reactions only ever arrive as live pushes while the bot is running: Telegram refuses
    to let a bot read a message's reactions back, and a user session only sees aggregate
    counts with no identity. So a reaction given while we were down is invisible, and the
    window has to be recorded so a check can refuse to punish during it.

    Comparing the heartbeat to a drop's timestamp would be useless, because the heartbeat
    is always newer than every past drop.
    """
    row = dbm.q1(conn, "SELECT value FROM kv WHERE key='hb:queue_bot'")
    now = datetime.now(timezone.utc)
    if row and row["value"]:
        try:
            previous = datetime.fromisoformat(row["value"])
        except (TypeError, ValueError):
            previous = None
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            gap = int((now - previous).total_seconds())
            if gap > grace_seconds:
                dbm.ex(
                    conn,
                    "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                    ("down:from", previous.isoformat()),
                )
                dbm.ex(
                    conn,
                    "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                    ("down:to", now.isoformat()),
                )
                return gap
    dbm.ex(conn, "DELETE FROM kv WHERE key IN ('down:from','down:to')")
    return 0


def reaction_gap(conn, drop_created_at: str | None) -> int:
    """Seconds of recorded downtime that happened after this drop was created.

    Zero means we saw every reaction as it happened, so a missing heart really is a
    missing heart and the member can be judged. Anything larger means our own outage
    could have hidden a real reaction, so the check has to stop at review.
    """
    if not drop_created_at:
        return 0
    start = dbm.q1(conn, "SELECT value FROM kv WHERE key='down:from'")
    end = dbm.q1(conn, "SELECT value FROM kv WHERE key='down:to'")
    if not start or not end:
        return 0
    try:
        down_from = datetime.fromisoformat(start["value"])
        down_to = datetime.fromisoformat(end["value"])
        created = datetime.fromisoformat(drop_created_at)
    except (TypeError, ValueError):
        return 0
    for d in (down_from, down_to, created):
        if d.tzinfo is None:
            d.replace(tzinfo=timezone.utc)
    if down_from.tzinfo is None:
        down_from = down_from.replace(tzinfo=timezone.utc)
    if down_to.tzinfo is None:
        down_to = down_to.replace(tzinfo=timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    overlap_start = max(down_from, created)
    if down_to <= overlap_start:
        return 0
    return int((down_to - overlap_start).total_seconds())


def reacted_on(conn, drop_id: int, tg_user_id: int):
    """True when this member put a heart on that entry.

    Only the heart family counts. Any other reaction is ignored rather than accepted,
    so a stray fire emoji cannot stand in for real engagement.
    """
    rows = dbm.q(
        conn,
        "SELECT emoji FROM reactions WHERE drop_id=? AND reactor_tg_user_id=?",
        (drop_id, tg_user_id),
    )
    return any(is_allowed_emoji(r["emoji"]) for r in rows)


def mark_comments_closed(conn, drop_id: int) -> None:
    """Remember that this link can never be commented on, so it leaves the window."""
    conn.execute("UPDATE drops SET comments_open=0 WHERE id=?", (drop_id,))
    conn.commit()




async def judge_links_async(conn, sample, identity, reader, need_comment=True) -> dict:
    """Same as judge_links, but the reader is awaited instead of called."""
    member_handle = (identity or {}).get("x_handle")
    tg_id = (identity or {}).get("tg_user_id")

    results = []
    for row in sample:
        tweet_id = row["tweet_id"]
        owner_handle = _field(row, "owner_handle")
        liked = reacted_on(conn, row["id"], tg_id) if tg_id else False

        if not need_comment:
            opened = {"tweet_id": tweet_id, "url": None, "replied": None,
                      "complete": True, "open": True, "skipped": True}
        else:
            read_async = getattr(reader, "read_async", None)
            if read_async is None:
                opened = reader(tweet_id, owner_handle, member_handle)
            else:
                opened = await read_async(tweet_id, owner_handle, member_handle)
            if opened.get("open") is False:
                mark_comments_closed(conn, row["id"])

        state = link_state(
            liked,
            opened.get("replied"),
            comments_open=opened.get("open", True),
            read_complete=opened.get("complete", False),
            need_comment=need_comment,
        )
        results.append({
            "tweet_id": tweet_id,
            "url": opened.get("url"),
            "owner_handle": owner_handle,
            "liked": liked,
            "replied": opened.get("replied"),
            "comments_open": opened.get("open", True),
            "read_complete": opened.get("complete", False),
            "needs_comment": need_comment,
            "state": state,
        })

    states = [r["state"] for r in results]
    outcome = verdict(states)
    return {
        "verdict": outcome,
        "links": results,
        "checked": len(results),
        "defaulter": outcome == "violated",
    }


def judge_links(conn, sample, identity, reader, need_comment=True) -> dict:
    """Read each sampled link once and turn the result into a verdict.

    `reader` is called as reader(tweet_id, owner_handle, member_handle) and must return
    a dict with `replied` (bool or None), `complete` (bool) and `open` (bool).

    With `need_comment` False the circle only asks for a heart, so X is never opened.
    """
    member_handle = (identity or {}).get("x_handle")
    tg_id = (identity or {}).get("tg_user_id")

    results = []
    for row in sample:
        tweet_id = row["tweet_id"]
        owner_handle = _field(row, "owner_handle")
        liked = reacted_on(conn, row["id"], tg_id) if tg_id else False

        if not need_comment:
            opened = {"tweet_id": tweet_id, "url": None, "replied": None,
                      "complete": True, "open": True, "skipped": True}
        else:
            opened = reader(tweet_id, owner_handle, member_handle)
            if opened.get("open") is False:
                mark_comments_closed(conn, row["id"])

        state = link_state(
            liked,
            opened.get("replied"),
            comments_open=opened.get("open", True),
            read_complete=opened.get("complete", False),
            need_comment=need_comment,
        )
        results.append({
            "tweet_id": tweet_id,
            "url": opened.get("url"),
            "owner_handle": owner_handle,
            "liked": liked,
            "replied": opened.get("replied"),
            "comments_open": opened.get("open", True),
            "read_complete": opened.get("complete", False),
            "needs_comment": need_comment,
            "state": state,
        })

    states = [r["state"] for r in results]
    outcome = verdict(states)
    return {
        "verdict": outcome,
        "links": results,
        "checked": len(results),
        "defaulter": outcome == "violated",
    }