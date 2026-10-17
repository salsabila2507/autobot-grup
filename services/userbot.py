"""User session service: the ONLY component that sends /warn, /mute, /ban to MissRose.

Design notes (locked):
- One Telegram session per process. This file owns the StringSession; queue_bot owns the bot token.
- MissRose is the strike authority; the engine only mirrors. So we never mutate strikes here.
- enforcement.mode=list_only means intents stay queued and are never sent.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from engine import config as cfgmod
from engine import db as dbm
from engine import identity as idm
from engine import outbox

log = logging.getLogger("userbot")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def heartbeat(conn):
    dbm.ex(
        conn,
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
        ("hb:userbot", utcnow().isoformat(timespec="seconds")),
    )


def resolve_target(conn, identity_id: int):
    """Return the @username of the identity's primary account, or None."""
    row = dbm.q1(
        conn,
        "SELECT a.tg_username FROM accounts a WHERE a.identity_id=? AND a.status='active' "
        "ORDER BY a.id LIMIT 1",
        (identity_id,),
    )
    if row is None or not row["tg_username"]:
        return None
    return row["tg_username"]


def build_command(conn, row) -> str | None:
    """MissRose command text for a queued punish intent. Read-only, no side effects."""
    username = resolve_target(conn, row["identity_id"])
    if username is None:
        return None
    level = row["level"] if row["level"] else 1
    kind = row["kind"]
    duration = row["duration"]
    if kind == "warn":
        return f"/warn @{username} {row['reason'] or 'missed engagements'}"
    if kind == "mute":
        return f"/mute @{username} {duration or '4h'}"
    if kind == "ban":
        return f"/ban @{username} {row['reason'] or ''}".strip()
    return None


def intents_for(conn, mode: str, limit: int = 5):
    """list_only never sends: return an empty list so intents stay queued."""
    if mode == "list_only":
        return []
    return outbox.pending_punish(conn, limit=limit)

class UserBot:
    def __init__(self, cfg, conn, client, group):
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self.group = group

    async def send_intent(self, row) -> bool:
        command = build_command(self.conn, row)
        if command is None:
            outbox.mark_punish_failed(self.conn, row["id"], "no resolvable target account")
            return False
        try:
            await self.client.send_message(self.group, command)
        except errors.FloodWaitError as exc:
            outbox.bump_punish_attempt(self.conn, row["id"], f"floodwait:{exc.seconds}")
            await asyncio.sleep(exc.seconds + 1)
            return False
        except errors.ChatAdminRequiredError:
            outbox.bump_punish_attempt(self.conn, row["id"], "chat_admin_required")
            self.raise_alert(row["id"], "userbot is not admin in the group, cannot drive MissRose")
            return False
        except errors.RPCError as exc:
            outbox.bump_punish_attempt(self.conn, row["id"], f"{type(exc).__name__}:{exc}")
            log.error("send failed intent=%s err=%s", row["id"], exc)
            return False
        outbox.mark_punish_sent(self.conn, row["id"])
        log.info("sent intent=%s case=%s mode=%s", row["id"], row["case_id"], row["mode"])
        return True

    def raise_alert(self, intent_id, reason: str):
        text_value = f"userbot alert: {reason} (intent {intent_id}) at {utcnow().isoformat(timespec='seconds')}"
        outbox.enqueue(self.conn, self.group.key, "alert", {"chat_id": self.group.mod_group, "text": text_value})

    async def loop(self):
        while True:
            try:
                rows = intents_for(self.conn, self.cfg.enforcement.mode, limit=5)
                for row in rows:
                    await self.send_intent(row)
            except Exception as exc:
                log.error("userbot loop error: %s", exc)
            heartbeat(self.conn)
            await asyncio.sleep(5)


def load_config_and_db():
    path = os.environ.get(
        "HOTTI_CONFIG",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
    )
    cfg = cfgmod.load(path)
    conn = dbm.connect(
        os.environ.get("HOTTI_DB", os.path.join(os.path.dirname(path), "data", "engine.sqlite"))
    )
    return cfg, conn


async def amain():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg, conn = load_config_and_db()
    if not cfg.groups:
        raise SystemExit("no groups configured")
    group = cfg.groups[0]
    session_string = cfgmod.read_secret(cfg, "userbot_session")
    client = TelegramClient(StringSession(session_string), cfg.app.api_id, cfg.app.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("userbot session is not authorized, re-hand the session string")
    me = await client.get_me()
    log.info("userbot ready id=%s username=%s enforcement=%s", me.id, me.username, cfg.enforcement.mode)

    bot = UserBot(cfg, conn, client, group)
    asyncio.create_task(bot.loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(amain())
