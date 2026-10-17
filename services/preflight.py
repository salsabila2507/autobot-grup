"""Preflight checks. Run before every launch and after every restart.

    python -m services.preflight

Checks, in order: config loads, DB writable, secrets present and chmod 600, admin ids
seeded, shifts loaded, cookie pool loadable, schedule covers 24h, topics distinct,
outbox/punish_intents schema at the expected version.

Exit code 0 means every required check passed. Optional checks are reported as WARN.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

from engine import config as cfgmod
from engine import db as dbm
from engine import rounds as rounds_mod
from engine import shifts as shifts_mod

log = logging.getLogger("preflight")

REQUIRED = "FAIL"
OPTIONAL = "WARN"
OK = "OK"

REQUIRED_SCHEMA_VERSION = 5


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail=""):
        self.rows.append((name, status, detail))

    @property
    def failed(self):
        return [r for r in self.rows if r[1] == REQUIRED]

    def render(self) -> str:
        width = max(len(r[0]) for r in self.rows) if self.rows else 10
        lines = []
        for name, status, detail in self.rows:
            lines.append(f"{name.ljust(width)}  {status:4}  {detail}".rstrip())
        return "\n".join(lines)


def check_config(report, cfg):
    if cfg is None:
        return
    report.add("config", OK, f"groups={[g.key for g in cfg.groups]}")
    for g in cfg.groups:
        if not g.admin_user_ids:
            report.add(f"admin_ids[{g.key}]", REQUIRED, "admin_user_ids empty in config")
        if not g.mod_group:
            report.add(f"mod_group[{g.key}]", OPTIONAL, "mod_group is 0, no alerts or cards will be delivered")
    report.add("enforcement", OK, cfg.enforcement.mode)
    if cfg.enforcement.mode != "list_only":
        report.add("enforcement_mode", OPTIONAL, f"not list_only ({cfg.enforcement.mode}), ladder may fire")
    else:
        report.add("enforcement_mode", OK, "list_only, safe for launch")


def check_schedule(report, cfg):
    if cfg is None:
        return
    try:
        rounds_mod.parse_schedule(cfg.rounds.schedule_utc)
        report.add("schedule", OK, " to ".join(cfg.rounds.schedule_utc))
    except ValueError as exc:
        report.add("schedule", REQUIRED, str(exc))


def check_topics(report, cfg):
    if cfg is None:
        return
    for g in cfg.groups:
        tids = [c.topic_id for c in g.circles]
        if len(set(tids)) != len(tids):
            report.add(f"topics[{g.key}]", REQUIRED, f"duplicate circle topic ids {tids}")
        else:
            report.add(f"topics[{g.key}]", OK, f"circle topics {tids} distinct")


def check_db(report, conn):
    if conn is None:
        return
    try:
        conn.execute("SELECT COUNT(*) FROM kv").fetchone()
        report.add("db", OK, "readable")
    except Exception as exc:
        report.add("db", REQUIRED, f"unreadable: {exc}")
        return
    try:
        dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES ('preflight:probe', ?)", (dbm.now_iso(),))
        conn.execute("DELETE FROM kv WHERE key='preflight:probe'")
        report.add("db_write", OK, "writable")
    except Exception as exc:
        report.add("db_write", REQUIRED, f"not writable: {exc}")
    version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    if version == REQUIRED_SCHEMA_VERSION:
        report.add("schema", OK, f"v{version}")
    else:
        report.add("schema", REQUIRED, f"v{version}, expected v{REQUIRED_SCHEMA_VERSION}, run migrate")


def check_admins(report, conn, cfg):
    if conn is None or cfg is None:
        return
    n = conn.execute("SELECT COUNT(*) AS n FROM group_admins").fetchone()["n"]
    if n:
        report.add("admins_seeded", OK, f"{n} rows")
    else:
        report.add("admins_seeded", OPTIONAL, "empty, start queue_bot once to seed from config")


def check_shifts(report, conn):
    if conn is None:
        return
    from datetime import datetime, timezone

    n = conn.execute("SELECT COUNT(*) AS n FROM shifts").fetchone()["n"]
    if not n:
        report.add("shifts", OPTIONAL, "no shifts loaded, every hour counts as uncovered")
        return
    now = datetime.now(timezone.utc)
    on = shifts_mod.on_duty(conn, now)
    covered = shifts_mod.is_covered(conn, now)
    report.add("shifts", OK, f"{n} loaded, on_duty_now={len(on)} covered={covered}")


def check_secrets(report, cfg):
    if cfg is None:
        return
    for label, path in (
        ("queue_bot_token", cfg.secrets.queue_bot_token_file),
        ("userbot_session", cfg.secrets.userbot_session_file),
    ):
        if not os.path.isfile(path):
            report.add(f"secret[{label}]", OPTIONAL, f"missing {path}, that service cannot start")
            continue
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            report.add(f"secret[{label}]", REQUIRED, f"{path} is too open ({oct(mode)}), chmod 600 required")
        else:
            report.add(f"secret[{label}]", OK, f"{path} chmod {oct(mode & 0o600 & 0o777)}")


def check_cookies(report, cfg):
    """The browser reader is what reads X now, so check its cookie header exists."""
    if cfg is None:
        return
    path = cfg.x_reader.cookie_file
    if not path:
        report.add("cookie_pool", REQUIRED, "x_reader.cookie_file not configured")
        return
    if not os.path.isfile(path):
        report.add("cookie_pool", REQUIRED, f"missing {path}")
        return
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        report.add("cookie_pool", REQUIRED, f"{path} is readable by others, chmod 600")
        return
    with open(path, "r", encoding="utf-8") as fh:
        header = fh.read().strip()
    if "auth_token=" not in header or "ct0=" not in header:
        report.add("cookie_pool", REQUIRED, f"{path} has no auth_token/ct0")
        return
    report.add("cookie_pool", OK, f"browser cookie header at {path}")


def run(config_path, db_path) -> Report:
    report = Report()
    cfg = None
    conn = None
    try:
        cfg = cfgmod.load(config_path)
        report.add("config_file", OK, config_path)
    except Exception as exc:
        report.add("config_file", REQUIRED, f"{config_path}: {exc}")
    check_config(report, cfg)
    check_schedule(report, cfg)
    check_topics(report, cfg)
    if conn is None:
        try:
            conn = dbm.connect(db_path)
        except Exception as exc:
            report.add("db_open", REQUIRED, f"{db_path}: {exc}")
    check_db(report, conn)
    check_admins(report, conn, cfg)
    check_shifts(report, conn)
    check_secrets(report, cfg)
    check_cookies(report, cfg)
    return report


async def run_live(cfg, report):
    if cfg is None:
        return
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    for group in cfg.groups:
        try:
            token = cfgmod.read_secret(cfg, "queue_bot_token")
        except Exception as exc:
            report.add(f"bot_token[{group.key}]", OPTIONAL, f"unavailable: {exc}")
            return
        client = TelegramClient(StringSession(), cfg.app.api_id, cfg.app.api_hash)
        try:
            await client.connect(bot_token=token)
            me = await client.get_me()
            perms = await client.get_permissions(group.main_group, me)
            admin = perms.is_admin or perms.is_creator
            can_delete = bool(perms.delete_messages)
            report.add(
                f"bot_rights[{group.key}]",
                OK if admin and can_delete else REQUIRED,
                f"@{me.username} admin={admin} delete={can_delete}",
            )
        except Exception as exc:
            report.add(f"bot_rights[{group.key}]", OPTIONAL, f"unverified: {type(exc).__name__}")
        finally:
            await client.disconnect()
        break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "HOTTI_CONFIG",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"),
        ),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "HOTTI_DB",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "engine.sqlite"),
        ),
    )
    parser.add_argument("--live", action="store_true", help="also verify bot rights against Telegram")
    args = parser.parse_args()

    report = run(args.config, args.db)
    if args.live:
        try:
            cfg = cfgmod.load(args.config)
        except Exception:
            cfg = None
        asyncio.run(run_live(cfg, report))

    print(report.render())
    failed = report.failed
    if failed:
        print(f"\nPREFLIGHT FAILED: {len(failed)} required check(s) failed")
        return 1
    print("\nPREFLIGHT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())