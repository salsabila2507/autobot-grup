import re
from dataclasses import dataclass

from engine import db as dbm


@dataclass
class ParsedAction:
    kind: str
    level: int | None
    duration: str | None
    target_tg_user_id: int | None
    target_username: str | None


_PATTERNS = [
    (
        re.compile(r"(?P<user>.+?) has been warned \((?P<level>\d)/3 warns\)"),
        lambda m: ParsedAction("warn", int(m.group("level")), None, None, m.group("user")),
    ),
    (
        re.compile(r"That's (?P<level>\d)/3 warnings; (?P<user>.+?) is muted for (?P<dur>\d+ \w+)"),
        lambda m: ParsedAction("mute", int(m.group("level")), m.group("dur"), None, m.group("user")),
    ),
    (
        re.compile(r"Muted (?P<user>.+?) for (?P<dur>\d+ days)"),
        lambda m: ParsedAction("mute", None, m.group("dur"), None, m.group("user")),
    ),
    (
        re.compile(r"Muted (?P<user>.+?) for (?P<dur>\d+ hours)"),
        lambda m: ParsedAction("mute", None, m.group("dur"), None, m.group("user")),
    ),
    (
        re.compile(r"Shhh\.\.\. quiet now\. Muted (?P<user>.+?) for (?P<dur>\d+ \w+)"),
        lambda m: ParsedAction("mute", None, m.group("dur"), None, m.group("user")),
    ),
    (
        re.compile(r"Banned (?P<user>.+?)(?:\.|$)"),
        lambda m: ParsedAction("ban", None, None, None, m.group("user")),
    ),
    (
        re.compile(r"Shunned (?P<user>.+?)(?:\.|$)"),
        lambda m: ParsedAction("ban", None, None, None, m.group("user")),
    ),
    (
        re.compile(r"/(?:warn|mute|ban)\s+(?:@)?(?P<user>[A-Za-z0-9_]{4,})(?:\s+(?P<dur>\d+[hd]))?"),
        lambda m: ParsedAction(
            "warn" if m.group(0).startswith("/warn") else ("mute" if m.group(0).startswith("/mute") else "ban"),
            None,
            m.group("dur"),
            None,
            m.group("user"),
        ),
    ),
]

_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,})")


def parse_line(text_value: str) -> ParsedAction | None:
    for pattern, builder in _PATTERNS:
        m = pattern.search(text_value or "")
        if m:
            action = builder(m)
            um = _USERNAME_RE.search(m.group("user") or "")
            if um:
                action.target_username = um.group(1)
            return action
    return None


def ingest(conn, msg_text: str, source_bot: str = "MissRose_bot", observed_at: str | None = None) -> int:
    action = parse_line(msg_text)
    status = "parsed" if action else "unparsed"
    target_id = None
    if action and action.target_username:
        row = dbm.q1(conn, "SELECT id FROM accounts WHERE tg_username=?", (action.target_username,))
        if row:
            target_id = row["id"]
    eid = dbm.ex(
        conn,
        "INSERT INTO external_actions (source_bot, raw_text, status, target_tg_user_id, kind, level, duration, observed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            source_bot,
            msg_text,
            status,
            target_id,
            action.kind if action else None,
            action.level if action else None,
            action.duration if action else None,
            observed_at or dbm.now_iso(),
        ),
    )
    if action and target_id:
        row = dbm.q1(
            conn,
            "SELECT i.id AS iid, i.strikes FROM identities i JOIN accounts a ON a.identity_id=i.id WHERE a.id=?",
            (target_id,),
        )
        if row:
            new_strikes = row["strikes"]
            if action.kind == "warn" and action.level:
                new_strikes = max(new_strikes, action.level)
            dbm.ex(conn, "UPDATE identities SET strikes=? WHERE id=?", (new_strikes, row["iid"]))
    return eid
