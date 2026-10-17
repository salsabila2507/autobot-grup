"""Sampled engagement check.

The checker never reads all 30 required links. It takes a few random links from the
member's window and judges on those, which keeps the request count near 2 per member
per round instead of 30.

Three outcomes, so a member is never auto-punished on a coin flip:
  clear     every sampled link engaged
  violated  every sampled link missing, strong evidence
  mixed     some engaged, some not, needs a human
"""

from engine import checker
from engine import db as dbm
from engine import engine as eng
from engine import lookup


def judge(engaged_flags: list[bool]) -> str:
    if not engaged_flags:
        return "unknown"
    if all(engaged_flags):
        return "clear"
    if not any(engaged_flags):
        return "violated"
    return "mixed"


async def evaluate_sampled_async(conn, identity_id, circle, size, rng, reader,
                                 need_comment=None, confirm=True) -> dict:
    """Async twin of evaluate_sampled, for callers already on an event loop."""
    if need_comment is None:
        need_comment = getattr(circle, "kind", "engagement") in checker.NEEDS_COMMENT_KINDS

    prepared = lookup.sample_links(conn, identity_id, circle, size, rng)
    if not prepared["found"]:
        return {"verdict": "unknown", "reason": prepared["reason"], "links": []}

    identity = lookup.identity_for_check(conn, identity_id)
    first = await checker.judge_links_async(conn, prepared["sample"], identity, reader,
                                            need_comment=need_comment)
    gap = checker.reaction_gap(conn, _created_at(prepared.get("drop")))
    if gap:
        first = dict(first)
        first["verdict"] = "review"
        first["defaulter"] = False
        first["downtime_seconds"] = gap
    result = {
        "verdict": first["verdict"],
        "links": first["links"],
        "drop": prepared["drop"],
        "identity": identity,
        "checked": first["checked"],
        "window_total": len(prepared["above"]),
        "confirmed": False,
        "downtime_seconds": first.get("downtime_seconds", 0),
    }
    if first["verdict"] != "violated" or not confirm:
        return result

    sampled = {link["tweet_id"] for link in first["links"]}
    remaining = [r for r in prepared["above"] if r["tweet_id"] not in sampled]
    if len(remaining) < size:
        result["verdict"] = "review"
        result["reason"] = "not enough links left to confirm"
        return result

    second = await checker.judge_links_async(conn, rng.sample(remaining, size), identity,
                                             reader, need_comment=need_comment)
    result["links"] = first["links"] + second["links"]
    result["checked"] = len(result["links"])
    result["confirmed"] = second["verdict"] == "violated"
    result["confirm_verdict"] = second["verdict"]
    result["verdict"] = "violated" if result["confirmed"] else "review"
    if not result["confirmed"]:
        result["reason"] = "second sample did not confirm"
    return result


def open_case_for(conn, result, rules, identity_id):
    """Turn a confirmed violation into a real case.

    Nothing reached this before: cases were only ever opened by the old full-window
    scorer, which lives in a service that cannot authenticate against X. Without this
    the obligation was tracked but never judged, so nothing could ever be punished.

    Only a `violated` verdict gets here, which by construction means two disjoint
    samples both failed with every link proven, never on a partial read.
    """
    if result.get("verdict") != "violated":
        return None

    drop = result.get("drop")
    drop_id = checker._field(drop, "id") if drop is not None else None
    if drop_id is None:
        return None

    existing = dbm.q1(
        conn,
        "SELECT id FROM cases WHERE drop_id=? AND status IN (?,?)",
        (drop_id, dbm.CASE_OPEN, dbm.CASE_ACTIONED),
    )
    if existing:
        return existing["id"]

    evidence = {
        "method": "sampled",
        "sample_size": result.get("checked"),
        "confirmed": bool(result.get("confirmed")),
        "window_total": result.get("window_total"),
        "links": [
            {
                "tweet_id": link["tweet_id"],
                "state": link.get("state"),
                "liked": link.get("liked"),
                "replied": link.get("replied"),
                "read_complete": link.get("read_complete"),
            }
            for link in result.get("links", [])
        ],
    }
    return eng.open_case(conn, drop_id, int(getattr(rules, "miss_threshold", 2) or 2), evidence).id


def _created_at(drop):
    if drop is None:
        return None
    try:
        return drop["created_at"]
    except (KeyError, IndexError, TypeError):
        return None


def evaluate_sampled(conn, identity_id, circle, size, rng, reader, confirm=True) -> dict:
    """Judge a member on random links, confirmed with a second sample before punishing.

    Sampling two links out of thirty is cheap, but it also means a member who genuinely
    engaged on most of their window can still land on two misses by chance. Punishing on
    that alone is how an honest member gets silenced, so a "violated" first sample is
    never acted on by itself: a second, disjoint sample has to fail too.
    """
    prepared = lookup.sample_links(conn, identity_id, circle, size, rng)
    if not prepared["found"]:
        return {"verdict": "unknown", "reason": prepared["reason"], "links": []}

    identity = lookup.identity_for_check(conn, identity_id)
    need_comment = getattr(circle, "kind", "engagement") in checker.NEEDS_COMMENT_KINDS
    first = checker.judge_links(conn, prepared["sample"], identity, reader,
                                need_comment=need_comment)
    gap = checker.reaction_gap(conn, _created_at(prepared.get("drop")))
    if gap:
        first = dict(first)
        first["verdict"] = "review"
        first["defaulter"] = False
        first["downtime_seconds"] = gap
    result = {
        "verdict": first["verdict"],
        "links": first["links"],
        "drop": prepared["drop"],
        "identity": identity,
        "checked": first["checked"],
        "window_total": len(prepared["above"]),
        "confirmed": False,
        "downtime_seconds": first.get("downtime_seconds", 0),
    }
    if first["verdict"] != "violated" or not confirm:
        return result

    sampled = {link["tweet_id"] for link in first["links"]}
    remaining = [r for r in prepared["above"] if r["tweet_id"] not in sampled]
    if len(remaining) < size:
        result["verdict"] = "review"
        result["reason"] = "not enough links left to confirm"
        return result

    second = checker.judge_links(conn, rng.sample(remaining, size), identity, reader,
                                 need_comment=need_comment)
    result["links"] = first["links"] + second["links"]
    result["checked"] = len(result["links"])
    result["confirmed"] = second["verdict"] == "violated"
    result["confirm_verdict"] = second["verdict"]
    result["verdict"] = "violated" if result["confirmed"] else "review"
    if not result["confirmed"]:
        result["reason"] = "second sample did not confirm"
    return result




def verdict_line(result: dict) -> str:
    v = result["verdict"]
    checked = result.get("checked", 0)
    total = result.get("window_total", 0)
    if v == "clear":
        return f"PASS {checked}/{checked} sampled links engaged (window {total})"
    if v == "violated":
        return f"MISS confirmed on 2 samples, {checked} links missing (window {total})"
    if v == "review":
        miss = sum(1 for link in result.get("links", []) if link.get("state") == checker.MISS)
        unknown = sum(1 for link in result.get("links", []) if link.get("state") == checker.UNKNOWN)
        extra = []
        if result.get("downtime_seconds"):
            extra.append(f"bot was offline {result['downtime_seconds'] // 60} min")
        if result.get("confirm_verdict"):
            extra.append(f"second sample={result['confirm_verdict']}")
        if unknown:
            extra.append(f"{unknown} unreadable")
        tail = f" ({', '.join(extra)})" if extra else ""
        return f"REVIEW {miss} missing, {checked} sampled, window {total}{tail}"
    if v == "mixed":
        n = sum(1 for link in result.get("links", []) if link.get("engaged"))
        return f"PARTIAL {n}/{checked} sampled links engaged (window {total})"
    return f"UNKNOWN: {result.get('reason', 'no data')}"


def build_card(result: dict) -> tuple[str, list[list[dict]]]:
    """Review card text plus buttons that jump to each tweet."""
    identity = result.get("identity") or {}
    drop = result.get("drop") or {}
    if not isinstance(drop, dict):
        drop = {k: drop[k] for k in drop.keys()} if hasattr(drop, "keys") else {}
    if not isinstance(identity, dict):
        identity = {k: identity[k] for k in identity.keys()} if hasattr(identity, "keys") else {}
    handle = identity.get("x_handle")
    lines = [
        f"Sampled check @{handle}" if handle else "Sampled check",
        f"Drop #{drop.get('position')} in circle, window {result.get('window_total')} links",
        f"Sampled {result.get('checked', 0)} links: {verdict_line(result)}",
        "",
    ]
    if result.get("reason"):
        lines.insert(3, f"Reason: {result['reason']}")
    buttons = []
    for link in result.get("links", []):
        state = link.get("state")
        mark = {"engaged": "OK", "miss": "MISS"}.get(state, "?")
        react = "yes" if link.get("liked") else "no"
        reply = link.get("replied")
        reply_txt = "yes" if reply else ("no" if reply is False else "unread")
        open_txt = "" if link.get("comments_open", True) else " COMMENTS CLOSED"
        lines.append(
            f"{mark} react={react} reply={reply_txt}{open_txt} {link['tweet_id']}"
        )
        if link.get("url"):
            buttons.append([{"text": f"Open {mark} {link['tweet_id'][-6:]}", "url": link["url"]}])
    return "\n".join(lines), buttons


# kept for callers that only want the verdict
