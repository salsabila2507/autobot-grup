"""One-off cleanup: remove the pile of duplicate 'read rules' dividers.

Earlier runs posted a fresh divider after every drop, so some topics ended up with many
copies stacked up. The bot now keeps exactly one, but the old ones are still sitting
there.

Only messages whose text starts with the divider line are touched. Entries, rejects and
anything a member wrote are never matched.

    python -m services.divider_cleanup --dry-run
    python -m services.divider_cleanup --keep-newest
"""

import argparse
import asyncio
import os
import sys

from telethon import TelegramClient

from engine import config as cfgmod
from engine import db as dbm
from engine import text as text_mod

PREFIX = "Quick engagement here"


def load_cfg():
    path = os.environ.get("HOTTI_CONFIG", "config.yaml")
    return cfgmod.load(path)


async def run(cfg, dry_run: bool, keep_newest: bool, as_userbot: bool = False) -> int:
    if as_userbot:
        from telethon.sessions import StringSession
        session = StringSession(open(cfg.secrets.userbot_session_file).read().strip())
        client = TelegramClient(session, cfg.app.api_id, cfg.app.api_hash)
        await client.connect()
    else:
        token = open(cfg.secrets.queue_bot_token_file).read().strip()
        session_path = os.path.join("data", "divider_cleanup")
        client = TelegramClient(session_path, cfg.app.api_id, cfg.app.api_hash)
        await client.start(bot_token=token)

    try:
        me = await client.get_me()
        print(f"acting as @{me.username}")
        removed = 0
        for group in cfg.groups:
            for circle in group.circles:
                total = 0
                hits = []
                async for msg in client.iter_messages(group.main_group, limit=400):
                    topic = getattr(msg, "reply_to_msg_id", None) or getattr(msg, "reply_to_top_id", None)
                    if topic != circle.topic_id:
                        continue
                    body = (msg.message or "").strip()
                    if not body.startswith(PREFIX):
                        continue
                    total += 1
                    hits.append((msg.id, body.splitlines()[0][:60]))

                if not hits:
                    continue

                doomed = hits[:-1] if (keep_newest and len(hits) > 1) else hits
                print(f"\n{group.key}/{circle.key} topic={circle.topic_id}: "
                      f"{total} divider(s) found, {len(doomed)} marked for deletion")
                for mid, first in doomed:
                    print(f"   - id={mid} {first}")
                    if not dry_run:
                        try:
                            await client.delete_messages(group.main_group, mid)
                            removed += 1
                        except Exception as exc:
                            print(f"     ! failed: {type(exc).__name__}: {exc}")
        if dry_run:
            print(f"\nDRY RUN, nothing deleted. Would remove {removed} message(s).")
        else:
            print(f"\ndeleted {removed} divider message(s)")
        return removed
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--go", dest="dry_run", action="store_false")
    parser.add_argument("--keep-newest", action="store_true", default=True)
    parser.add_argument("--as-userbot", action="store_true",
                        help="use the MissRose session; bots cannot read channel history")
    args = parser.parse_args()
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging_cfg = __import__("logging").basicConfig(level=__import__("logging").INFO)
    asyncio.run(run(load_cfg(), args.dry_run, args.keep_newest, args.as_userbot))


if __name__ == "__main__":
    main()