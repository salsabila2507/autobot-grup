"""Engagement scoring.

Two independent signals, both required per link:
  - like  : Telegram reaction on the numbered entry (free, authoritative for "like")
  - reply : a comment on X, read by x_reader (kind='reply' in engagement_reads)

A required link counts as engaged only when the member both reacted on Telegram and
commented on X. Missed count feeds the defaulter case; clearing the obligation feeds
the drop gate.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from engine import db as dbm


@dataclass
class Case:
    id: int
    identity_id: int
    drop_id: int
    missing_count: int
    status: str


@dataclass
class LinkSignal:
    tweet_id: str
    drop_id: int
    liked: bool
    replied: bool

    @property
    def engaged(self) -> bool:
        return self.liked and self.replied


def _case_from(row) -> Case:
    return Case(
        id=row["id"],
        identity_id=row["identity_id"],
        drop_id=row["drop_id"],
        missing_count=row["missing_count"],
        status=row["status"],
    )


















def open_case(conn, drop_id: int, missing_count: int, evidence: dict) -> Case:
    eid = dbm.ex(
        conn,
        "INSERT INTO cases (identity_id, drop_id, missing_count, evidence, status, opened_at) "
        "SELECT identity_id, ?, ?, ?, 'open', ? FROM drops WHERE id=?",
        (drop_id, missing_count, json.dumps(evidence), dbm.now_iso(), drop_id),
    )
    return _case_from(dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (eid,)))




