"""Check whether an X post's Likes tab can actually be read with the read-only cookie.

The question that matters is not "is there a likes tab", it is "can we enumerate the
users who liked a post", because the checker needs to answer one specific question:
did THIS member like THIS post. Sampling the first few likers tells us nothing.

Read only. Nothing is clicked that changes state on X.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.x_reader import browser


async def probe(handle: str, tweet_id: str, cookie: str, scrolls: int = 12) -> None:
    from playwright.async_api import async_playwright

    url = browser.tweet_url(handle, tweet_id)
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(
            user_agent=browser.user_agent(),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await ctx.add_cookies(browser.parse_cookie_header(cookie))
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            tabs = await page.eval_on_selector_all(
                "[role='tab']",
                "els => els.map(e => (e.innerText||'').trim()).filter(t => t)",
            )
            print(f"tabs visible: {tabs}")

            clicked = False
            for label in ("Likes", "Liked by"):
                locator = page.locator(f"[role='tab']:has-text('{label}')")
                if await locator.count() > 0:
                    await locator.first.click(timeout=5000)
                    clicked = True
                    print(f"clicked tab: {label}")
                    break
            if not clicked:
                print("RESULT: no Likes tab is reachable on this view")
                return

            await page.wait_for_timeout(3000)
            seen: set[str] = set()
            order: list[str] = []
            for i in range(1, scrolls + 1):
                found = await page.evaluate(
                    """() => Array.from(
                        document.querySelectorAll("a[href^='/'][role='link']")
                      ).map(a => (a.getAttribute('href')||'').replace(/^\\/+/, ''))
                       .filter(h => h && !h.includes('/') && h.length <= 15)"""
                )
                new = [h for h in found if h not in seen]
                for h in new:
                    seen.add(h)
                    order.append(h)
                await page.mouse.wheel(0, 1600)
                await page.wait_for_timeout(900)
                if i == scrolls:
                    break
            print(f"users collected after {scrolls} scrolls: {len(order)}")

            body = await page.inner_text("body")
            blocked = any(s in body.lower() for s in (
                "something went wrong", "log in", "sign up to", "retry",
            ))
            print(f"page text mentions login/error: {blocked}")
            if len(order) == 0:
                print("RESULT: likes cannot be enumerated from this view")
            else:
                print(f"RESULT: {len(order)} likers enumerated")
        finally:
            await b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("handle")
    ap.add_argument("tweet_id")
    ap.add_argument("--scrolls", type=int, default=12)
    args = ap.parse_args()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", browser.OWN_BROWSERS)
    cookie = browser.load_cookie("/home/ubuntu/.secrets/x_cookies/header_acct02.txt")
    asyncio.run(probe(args.handle, args.tweet_id, cookie, args.scrolls))


if __name__ == "__main__":
    main()
