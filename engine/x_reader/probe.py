"""Check whether a post still accepts public replies.

The syndication endpoint needs no login, which matters because our read-only accounts
cannot authenticate. When it reports `conversation_control` the author has turned
comments off, and that link must never sit in anyone's obligation window: nobody could
have commented on it, so counting it would manufacture a violation.
"""

import logging

log = logging.getLogger("probe")

SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def conversation_is_open(payload) -> bool:
    """False when replies are restricted, the post vanished, or it is protected."""
    if not isinstance(payload, dict):
        return False
    if payload.get("__typename") == "TweetUnavailable":
        return False
    if not payload.get("id_str"):
        return False
    control = payload.get("conversation_control")
    if isinstance(control, dict):
        policy = control.get("policy")
        if policy and policy != "All":
            return False
    return True


def probe_comments_open(tweet_id, client=None) -> bool:
    """True only when replies are confirmed open.

    This used to assume "open" whenever the probe failed, which meant a post whose
    comments were actually closed could still be pulled into a member's obligation and
    turned into a violation they could never have avoided. Failing closed is the safe
    direction here: the link simply drops out of the window and a human decides.
    """
    own = client is None
    if own:
        import httpx
        client = httpx.Client(follow_redirects=True, timeout=25)
    try:
        r = client.get(
            SYNDICATION,
            params={"id": str(tweet_id), "lang": "en", "token": "x"},
            headers={"user-agent": USER_AGENT},
        )
    except Exception as exc:
        log.warning("probe %s failed, treating as closed: %s", tweet_id, exc)
        return False
    finally:
        if own:
            client.close()

    if r.status_code != 200:
        log.warning("probe %s returned %s, treating as closed", tweet_id, r.status_code)
        return False
    try:
        return conversation_is_open(r.json())
    except Exception as exc:
        log.warning("probe %s bad body, treating as closed: %s", tweet_id, exc)
        return False