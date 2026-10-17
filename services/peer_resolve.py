"""Resolve Telegram peer ids for the queue bot without relying on its dialog cache.

Bots cannot call getDialogs, so a freshly started bot cannot resolve a group it was
added to until somebody mentions it or the group pushes something to it. This module
borrows the access_hash from the user session (which is already a member) and hands it
to the bot client, so the bot can operate on the group from the first second.
"""

import logging

log = logging.getLogger("peer_resolve")

CACHE_PATH = "/home/ubuntu/.secrets/hotti_peer_cache.json"


def _load():
    import json
    import os

    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    import json
    import os

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = f"{CACHE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)
    os.chmod(CACHE_PATH, 0o600)


def harvest_with_user_session(session_string: str, api_id: int, api_hash: str, chat_ids):
    """Resolve chat_ids from the user session, cache their access_hash."""
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession

    cache = _load()
    if isinstance(chat_ids, int):
        chat_ids = [chat_ids]
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    client.connect()
    try:
        for chat_id in chat_ids:
            ent = client.get_entity(chat_id)
            cache[str(chat_id)] = {
                "id": ent.id,
                "access_hash": ent.access_hash,
                "title": getattr(ent, "title", None),
            }
            log.info("peer harvested %s (%s) from %s", ent.id, getattr(ent, "title", None), chat_id)
    finally:
        client.disconnect()
    _save(cache)
    return cache


def prime_bot_session(bot_client, chat_id):
    """Push the cached access_hash into the bot session so get_entity works."""
    from telethon.tl.types import InputPeerChannel

    cache = _load()
    entry = cache.get(str(chat_id)) or cache.get(str(chat_id).lstrip("-100"))
    if not entry:
        return False
    key = int(chat_id)
    if str(key) != str(chat_id):
        key = int(str(chat_id).lstrip("-100"))
    peer = InputPeerChannel(channel_id=key, access_hash=entry["access_hash"])
    try:
        bot_client.session.add_input_entity(peer)
        log.info("bot session primed for %s", key)
        return True
    except Exception as exc:
        log.warning("priming failed for %s: %s", key, exc)
        return False



def main():
    import argparse
    import os

    from services import cfgmod_shim

    parser = argparse.ArgumentParser()
    parser.add_argument("--harvest", action="store_true", help="resolve peers using the user session and cache them")
    parser.add_argument("--config", default=os.environ.get("HOTTI_CONFIG", "config.yaml"))
    args = parser.parse_args()

    cfgmod_shim.load(args.config)
    cfg = cfgmod_shim.cfg
    session = cfgmod_shim.read_secret(cfg, "userbot_session")
    chat_ids = sorted({g.main_group for g in cfg.groups} | {g.mod_group for g in cfg.groups} - {0})
    if args.harvest:
        cache = harvest_with_user_session(session, cfg.app.api_id, cfg.app.api_hash, chat_ids)
        for k, v in cache.items():
            print(f"harvested {k}: id={v['id']} title={v['title']}")
    else:
        print(_load())


if __name__ == "__main__":
    main()
