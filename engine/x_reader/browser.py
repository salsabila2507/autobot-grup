"""Browser-backed reply reader.

The GraphQL API rejects our read-only accounts (401 code 32 "Could not
authenticate you", code 89 "Invalid or expired token"), so replies are read the way a
person reads them: open the post, switch the reply sort to chronological, scroll until
the list stops growing, and record every reply we saw.

Two details decide whether this is correct or harmful:

* X defaults replies to "Relevant", which hides a real share of them. On one test post
  "Relevant" showed 43 tweets and "Recent" showed 58, and a member who had genuinely
  replied only appeared under "Recent". Reading the default order would have turned a
  real comment into a false defaulter, so the sort is always switched.
* The conversation list is virtualised: rows leaving the viewport are removed from the
  DOM. Every scroll therefore re-collects and merges by tweet id, and a run is only
  reported complete when the page stopped moving and nothing new arrived.
* The focal post itself is dropped from the reply list. Its author naturally appears as
  the first article on the page, and counting that as a reply would let someone look
  engaged on their own post instead of on somebody else's.

Read only. Nothing is posted, liked, followed or written.
"""

import logging
import os

log = logging.getLogger("x_browser")

OWN_BROWSERS = "/home/ubuntu/bot-eg/hotti/.playwright"

# Playwright looks in ~/.cache/ms-playwright unless told otherwise, and the bot has its
# own browser there. Without this the launch fails with "Executable doesn't exist" and
# every check dies before reading a single reply.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", OWN_BROWSERS)

# Every rendered reply, collected in one pass.
ARTICLES_JS = """
() => {
  const out = [];
  document.querySelectorAll("article[data-testid='tweet']").forEach(a => {
    const link = a.querySelector("a[href*='/status/']");
    if (!link) return;
    const m = (link.getAttribute('href') || '').match(/\\/([A-Za-z0-9_]+)\\/status\\/(\\d+)/);
    if (!m) return;
    const nameEl = a.querySelector("[data-testid='User-Name'] a[href^='/']");
    const textEl = a.querySelector("[data-testid='tweetText']");
    out.push({
      id: m[2],
      handle: nameEl ? (nameEl.getAttribute('href') || '').replace(/^\\/+/, '') : '',
      text: textEl ? (textEl.innerText || '').trim() : ''
    });
  });
  return out;
}
"""


def parse_cookie_header(header):
    out = []
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(), "domain": ".x.com",
                    "path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"})
    return out


def user_agent():
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def tweet_url(handle, tweet_id):
    return f"https://x.com/{(handle or 'i').lstrip('@')}/status/{tweet_id}"


async def read_conversation(handle, tweet_id, cookie_header=None, headless=True,
                            max_scrolls=60, idle_rounds=4, pause=1.0, step=1200):
    """Read every reply on a post. Always bounded so a run terminates."""
    from playwright.async_api import async_playwright

    if not cookie_header:
        raise ValueError("a cookie header is required to read X")

    url = tweet_url(handle, tweet_id)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=user_agent(),
                                            viewport={"width": 1280, "height": 900},
                                            locale="en-US")
        await context.add_cookies(parse_cookie_header(cookie_header))
        page = await context.new_page()

        order = []
        seen = set()
        idle = 0
        scrolls = 0
        last_y = -1
        switched = False
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            switched = await _switch_to_recent(page)

            for scrolls in range(1, max_scrolls + 1):
                found = await page.evaluate(ARTICLES_JS)
                new = [a for a in found if a["id"] not in seen]
                for a in new:
                    seen.add(a["id"])
                    order.append(a)

                y = await page.evaluate("window.scrollY")
                bottom = await page.evaluate(
                    "window.scrollY + window.innerHeight >= document.body.scrollHeight - 50")
                log.info("scroll %2d/%d seen=%d new=%d y=%s%s",
                         scrolls, max_scrolls, len(order), len(new), y,
                         " [bottom]" if bottom else "")

                if new or y != last_y:
                    idle = 0
                else:
                    idle += 1
                    if idle >= idle_rounds:
                        break
                last_y = y

                if bottom:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                else:
                    await page.mouse.wheel(0, step)
                await page.wait_for_timeout(int(pause * 1000))
        finally:
            await browser.close()

    complete = idle >= idle_rounds
    focal = str(tweet_id)
    replies = [a for a in order if a["id"] != focal]
    return {
        "url": url,
        "tweet_id": focal,
        "author": (handle or "").lstrip("@").lower(),
        "replies": replies,
        "count": len(replies),
        "complete": complete,
        "sorted_recent": switched,
        "focal_seen": any(a["id"] == focal for a in order),
    }


async def _switch_to_recent(page):
    """Always leave the 'Relevant' default, it hides replies. X labels the option 'Recent'."""
    try:
        button = page.locator("button:has-text('Relevant'), "
                               "div[role='button']:has-text('Relevant')").first
        if await button.count() == 0:
            log.warning("sort control not found, replies may be incomplete")
            return False
        await button.click(timeout=5000)
        await page.wait_for_timeout(1500)
        for label in ("Recent", "Latest"):
            item = page.locator(f"[role='menuitem']:has-text('{label}')").first
            if await item.count() > 0:
                await item.click(timeout=5000)
                await page.wait_for_timeout(3000)
                log.info("sort switched to %s", label)
                return True
        log.warning("chronological sort option not found in menu")
        return False
    except Exception as exc:
        log.warning("could not switch sort: %s", exc)
        return False


def load_cookie(path):
    """Read a cookie header file. Never log the value."""
    with open(path) as fh:
        header = fh.read().strip()
    if "auth_token=" not in header:
        raise ValueError(f"{path} has no auth_token")
    return header