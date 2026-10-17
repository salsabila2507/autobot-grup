"""Turn a browser read into the verdict input the checker wants."""

import asyncio
import logging

from engine.x_reader import browser
from engine.x_reader.probe import probe_comments_open

log = logging.getLogger("comment_reader")


def make_reader(cookie_header, tweet_url=None, max_scrolls=60, idle_rounds=4,
                pause=1.0, step=1200):
    """Build a reader callable: reader(tweet_id, owner_handle, member_handle) -> dict.

    `owner_handle` is who posted the link and only builds the url. `member_handle` is
    the person being judged, and it is their comment we are looking for.

    Returns `replied` only when the thread was read end to end. A partial read gives
    None, which the checker treats as unknown and never as a defaulter.

    The scroll budget is passed through on purpose: a thread that runs out of scrolls
    reports incomplete rather than pretending it read everything.
    """
    url_of = tweet_url or browser.tweet_url

    async def read(tweet_id, owner_handle=None, member_handle=None):
        is_open = probe_comments_open(tweet_id)
        if not is_open:
            log.info("tweet %s has comments closed, skipping read", tweet_id)
            return {
                "tweet_id": str(tweet_id),
                "url": url_of(owner_handle, tweet_id),
                "replied": None,
                "complete": False,
                "open": False,
                "skipped": True,
            }

        result = await browser.read_conversation(
            owner_handle, tweet_id, cookie_header=cookie_header,
            max_scrolls=max_scrolls, idle_rounds=idle_rounds,
            pause=pause, step=step,
        )
        handles = [a["handle"].lower() for a in result["replies"] if a["handle"]]
        want = (member_handle or "").lstrip("@").lower()
        replied = None
        if result["complete"] and want:
            replied = want in handles
        return {
            "tweet_id": str(tweet_id),
            "url": result["url"],
            "replied": replied,
            "complete": result["complete"],
            "open": True,
            "count": result["count"],
            "sorted_recent": result["sorted_recent"],
            "handles": handles,
        }

    def reader(tweet_id, owner_handle=None, member_handle=None):
        """Synchronous entry point, for callers that are not already on an event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.new_event_loop().run_until_complete(
                read(tweet_id, owner_handle, member_handle))
        return asyncio.ensure_future(read(tweet_id, owner_handle, member_handle))

    reader.read_async = read
    return reader




