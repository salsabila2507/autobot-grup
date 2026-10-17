"""Scheduler service: round boundaries, dividers, deadlines, escalation.

No Telegram session in this process. Everything it needs to say is written to the
outbox table; queue_bot (the only bot-token holder) sends it.
"""

import logging
import os
import time
from datetime import datetime, timezone

from engine import config as cfgmod
from engine import db as dbm
from engine import escalation
from engine import outbox
from engine import rounds as rounds_mod
from engine import cards
from engine import text

log = logging.getLogger("scheduler")

TICK_SECONDS = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def heartbeat(conn):
    dbm.ex(
        conn,
        "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
        ("hb:scheduler", utcnow().isoformat(timespec="seconds")),
    )


def boundary_guard(conn, group_key: str, circle_key: str, round_id: int, now: datetime) -> bool:
    """True when the round boundary has not been processed yet. One shot per round."""
    key = f"boundary:{group_key}:{circle_key}:{round_id}"
    if dbm.q1(conn, "SELECT value FROM kv WHERE key=?", (key,)):
        return False
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, now.isoformat(timespec="seconds")))
    return True


def enqueue_divider(conn, group, circle, schedule, round_id, now: datetime) -> bool:
    marker = f"divider:{group.key}:{circle.key}:{round_id}"
    if dbm.q1(conn, "SELECT value FROM kv WHERE key=?", (marker,)):
        return False
    dbm.ex(conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (marker, dbm.now_iso()))
    outbox.enqueue(
        conn,
        group.key,
        "divider",
        {
            "chat_id": group.main_group,
            "topic_id": circle.topic_id,
            "text": text.divider_html(cards.rules_link(group), circle.kind),
            "circle_key": circle.key,
            "round_id": round_id,
        },
    )
    return True


class Scheduler:
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn

    def tick(self, now: datetime) -> dict:
        stats = {"dividers": 0, "warnings": 0, "escalations": 0}
        schedule = self.cfg.rounds.schedule_utc
        for group in self.cfg.groups:
            for circle in group.circles:
                round_row = rounds_mod.current_round(self.conn, circle.key, schedule, now)
                if boundary_guard(self.conn, group.key, circle.key, round_row.id, now):
                    rounds_mod.close_previous_rounds(self.conn, circle.key, schedule, now)
                    if enqueue_divider(self.conn, group, circle, schedule, round_row.id, now):
                        stats["dividers"] += 1
                    stats["warnings"] += self.boundary_warnings(group, circle, round_row, now)
            stats["escalations"] += self.escalate(group, now)
        return stats

    def boundary_warnings(self, group, circle, round_row, now: datetime) -> int:
        """One warning per open obligation at the boundary. Never for VIPs."""
        n = 0
        for row in escalation.open_obligations_before(self.conn, before=round_row.opened_at):
            result = escalation.boundary_warning(
                self.conn,
                group.key,
                self.cfg.rules,
                row,
                round_row.opened_at,
                now,
            )
            if result is None:
                continue
            n += 1
            identity = self.conn.execute(
                "SELECT tg_username FROM accounts WHERE identity_id=? AND status='active' ORDER BY id LIMIT 1",
                (result["identity_id"],),
            ).fetchone()
            mention = f"@{identity['tg_username']}" if identity and identity["tg_username"] else f"id {result['identity_id']}"
            line = text.PRISON_WARN_PUBLIC.format(
                mention=mention,
                level=result["level"],
                max=self.cfg.rules.max_warnings,
                duration=result["duration"],
                expires=now.isoformat(timespec="minutes")[11:16],
            )
            outbox.enqueue(
                self.conn,
                group.key,
                "public_warning",
                {
                    "chat_id": group.main_group,
                    "topic_id": group.topics["prison"],
                    "text": line,
                    "punishment_id": result["punishment_id"],
                },
            )
        return n

    def escalate(self, group, now: datetime) -> int:
        rows = escalation.cases_past_deadline(self.conn, group.key, self.cfg.rules, now)
        n = 0
        for row in rows:
            result = escalation.escalate_case(
                self.conn,
                group,
                self.cfg.rules,
                row,
                mode=self.cfg.enforcement.mode,
                now=now,
            )
            if result is None:
                continue
            n += 1
            log.info("escalated case=%s mode=%s level=%s", row["id"], self.cfg.enforcement.mode, result.get("level"))
        return n

    def run_forever(self):
        while True:
            started = time.time()
            try:
                stats = self.tick(utcnow())
                if any(stats.values()):
                    log.info("tick %s", stats)
            except Exception as exc:
                log.error("scheduler tick failed: %s", exc)
            heartbeat(self.conn)
            sleep_for = TICK_SECONDS - (time.time() - started)
            if sleep_for > 0:
                time.sleep(sleep_for)


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg, conn = load_config_and_db()
    Scheduler(cfg, conn).run_forever()


if __name__ == "__main__":
    main()