"""Find the drop a member is being judged on, and the links above it.

The checker never needs to know queue position. Give it a username and it walks back
to that member's latest drop, then collects the links posted above it.
"""

from engine import db as dbm


def find_identity(conn, username: str):
    """Resolve a Telegram username or X handle to an identity id.

    Matching is exact on purpose. A substring match let `/check i` resolve to whoever
    happened to be first, which meant a member could pull up somebody else's
    engagement data with a one letter guess. Nothing is returned unless it is
    unambiguous and exact.
    """
    handle = str(username or "").strip().lstrip("@").lower()
    if not handle:
        return None
    row = dbm.q1(
        conn,
        "SELECT identity_id FROM accounts WHERE LOWER(tg_username)=? AND status!='banned' ORDER BY id LIMIT 1",
        (handle,),
    )
    if row:
        return row["identity_id"]
    row = dbm.q1(
        conn,
        "SELECT id FROM identities WHERE LOWER(x_handle)=?",
        (handle,),
    )
    if row:
        return row["id"]
    return None


def find_identities(conn, username: str):
    """Every identity that could match, so callers can refuse an ambiguous guess."""
    handle = str(username or "").strip().lstrip("@").lower()
    if not handle:
        return []
    found = set()
    for row in dbm.q(
        conn, "SELECT identity_id FROM accounts WHERE LOWER(tg_username)=?", (handle,)
    ):
        found.add(row["identity_id"])
    for row in dbm.q(conn, "SELECT id FROM identities WHERE LOWER(x_handle)=?", (handle,)):
        found.add(row["id"])
    return sorted(found)


def latest_drop(conn, identity_id: int):
    """The drop that is currently being judged for this member."""
    return dbm.q1(
        conn,
        "SELECT * FROM drops WHERE identity_id=? AND status='active' ORDER BY position DESC LIMIT 1",
        (identity_id,),
    )


def links_above(conn, drop, circle) -> list[dict]:
    """The links posted above this drop in the same circle, oldest first.

    Links whose comments are known to be closed are skipped. Nobody can comment on
    those, so counting them would hand out unavoidable violations.
    """
    return dbm.q(
        conn,
        "SELECT d.id, d.position, d.tweet_id, d.identity_id, i.x_handle AS owner_handle "
        "FROM drops d LEFT JOIN identities i ON i.id=d.identity_id "
        "WHERE d.circle_key=? AND d.position<? AND d.status='active' "
        "AND COALESCE(d.comments_open, 1)=1 "
        "AND d.round_id=(SELECT round_id FROM drops WHERE id=?) "
        "ORDER BY d.position DESC LIMIT ?",
        (circle.key, drop["position"], drop["id"], circle.required),
    )


def sample_links(conn, identity_id: int, circle, size: int, rng) -> dict:
    """Everything the checker needs for one member, no position knowledge required."""
    identity = dbm.q1(conn, "SELECT * FROM identities WHERE id=?", (identity_id,))
    drop = latest_drop(conn, identity_id)
    if drop is None:
        return {"found": False, "reason": "no drop yet"}
    if circle is None:
        return {"found": False, "reason": "circle not configured"}
    above = links_above(conn, drop, circle)
    if not above:
        return {
            "found": True,
            "drop": drop,
            "identity": identity,
            "above": [],
            "sample": [],
            "reason": "no links above this drop yet",
        }
    picked = rng.sample(above, min(size, len(above)))
    return {
        "found": True,
        "drop": drop,
        "identity": identity,
        "above": above,
        "sample": picked,
    }


def identity_for_check(conn, identity_id: int) -> dict | None:
    """Identity plus the Telegram id, which lives on accounts rather than identities."""
    row = dbm.q1(
        conn,
        "SELECT i.id, i.x_handle, a.tg_user_id, a.tg_username "
        "FROM identities i LEFT JOIN accounts a ON a.identity_id=i.id "
        "WHERE i.id=?",
        (identity_id,),
    )
    return dict(row) if row else None


def tweet_url(handle: str, tweet_id: str) -> str:
    handle = (handle or "i").lstrip("@")
    return f"https://x.com/{handle}/status/{tweet_id}"


