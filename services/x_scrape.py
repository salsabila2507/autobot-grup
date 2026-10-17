"""CLI wrapper around the browser reply reader.

    python -m services.x_scrape <handle> <tweet_id> [--want @name] [--cookie-file PATH]

Read only. Never prints cookie values.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

from engine.x_reader.browser import OWN_BROWSERS, load_cookie, read_conversation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("handle")
    parser.add_argument("tweet_id")
    parser.add_argument("--want", action="append", help="handle to look for (repeatable)")
    parser.add_argument("--cookie-file", required=True)
    parser.add_argument("--max-scrolls", type=int, default=30)
    parser.add_argument("--idle-rounds", type=int, default=4)
    parser.add_argument("--pause", type=float, default=1.0)
    parser.add_argument("--json-out")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", OWN_BROWSERS)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stderr)

    result = asyncio.run(read_conversation(
        args.handle, args.tweet_id,
        cookie_header=load_cookie(args.cookie_file),
        headless=not args.headed,
        max_scrolls=args.max_scrolls,
        idle_rounds=args.idle_rounds,
        pause=args.pause,
    ))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=1)

    print(f"url      {result['url']}")
    print(f"replies  {result['count']} tweets read")
    print(f"complete {result['complete']}  (recent_sort={result['sorted_recent']})")
    if not result["complete"]:
        print("WARNING  read stopped early, nobody may be judged from this run")

    handles = [a["handle"].lower() for a in result["replies"] if a["handle"]]
    for want in args.want or []:
        w = want.lstrip("@").lower()
        print(f"@{w} replied: {'YES' if w in handles else 'NO'}")
    if args.show:
        for a in result["replies"]:
            print(f"  {a['handle']:<22} {a['text'][:70]!r}")


if __name__ == "__main__":
    main()