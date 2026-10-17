import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_V1 = """
CREATE TABLE identities (
  id INTEGER PRIMARY KEY,
  x_handle TEXT UNIQUE NOT NULL,
  x_user_id TEXT,
  vip INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  strikes INTEGER DEFAULT 0,
  blacklisted_at TEXT,
  first_seen_at TEXT NOT NULL
);

CREATE TABLE accounts (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER,
  tg_user_id INTEGER UNIQUE NOT NULL,
  tg_username TEXT,
  joined_at TEXT,
  invite_link TEXT,
  status TEXT DEFAULT 'active'
);

CREATE TABLE rounds (
  id INTEGER PRIMARY KEY,
  circle_key TEXT NOT NULL,
  seq INTEGER NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  UNIQUE(circle_key, opened_at)
);

CREATE TABLE drops (
  id INTEGER PRIMARY KEY,
  group_key TEXT NOT NULL,
  round_id INTEGER NOT NULL,
  circle_key TEXT NOT NULL,
  position INTEGER NOT NULL,
  identity_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  msg_id INTEGER,
  link TEXT NOT NULL,
  tweet_id TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  deleted_by INTEGER,
  deleted_reason TEXT,
  deleted_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE reactions (
  id INTEGER PRIMARY KEY,
  drop_id INTEGER NOT NULL,
  reactor_tg_user_id INTEGER NOT NULL,
  emoji TEXT,
  observed_at TEXT NOT NULL,
  UNIQUE(drop_id, reactor_tg_user_id)
);

CREATE TABLE engagement_reads (
  id INTEGER PRIMARY KEY,
  tweet_id TEXT NOT NULL,
  handle TEXT,
  x_user_id TEXT,
  kind TEXT NOT NULL,
  read_at TEXT NOT NULL,
  account_used TEXT,
  truncated INTEGER DEFAULT 0
);

CREATE TABLE cases (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  drop_id INTEGER NOT NULL,
  missing_count INTEGER NOT NULL,
  evidence TEXT,
  status TEXT DEFAULT 'open',
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  closed_by INTEGER,
  close_reason TEXT
);

CREATE TABLE punishments (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  case_id INTEGER,
  kind TEXT NOT NULL,
  level INTEGER,
  expires_at TEXT,
  reason TEXT,
  actor_tg_user_id INTEGER,
  mode TEXT NOT NULL,
  issued_at TEXT NOT NULL
);

CREATE TABLE external_actions (
  id INTEGER PRIMARY KEY,
  source_bot TEXT,
  raw_text TEXT,
  status TEXT DEFAULT 'parsed',
  target_tg_user_id INTEGER,
  kind TEXT,
  level INTEGER,
  duration TEXT,
  observed_at TEXT NOT NULL,
  linked_case_id INTEGER
);

CREATE TABLE alt_cases (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER,
  primary_account_id INTEGER,
  alt_account_id INTEGER,
  score INTEGER,
  signals TEXT,
  status TEXT DEFAULT 'open',
  decided_by INTEGER,
  decided_at TEXT
);

CREATE TABLE shifts (
  id INTEGER PRIMARY KEY,
  admin_tg_user_id INTEGER NOT NULL,
  tz TEXT NOT NULL,
  start_local TEXT NOT NULL,
  end_local TEXT NOT NULL,
  days_mask TEXT DEFAULT '1111111'
);

CREATE TABLE obligations (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  drop_id INTEGER NOT NULL,
  required INTEGER NOT NULL,
  cleared_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE early_drop_attempts (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  group_key TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  punished INTEGER DEFAULT 0
);

CREATE TABLE kv (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE INDEX idx_obligations_identity ON obligations(identity_id, cleared_at);
CREATE INDEX idx_drops_circle_pos ON drops(circle_key, position);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_reads_tweet ON engagement_reads(tweet_id, kind);
CREATE INDEX idx_drops_round ON drops(round_id, identity_id, tweet_id);
CREATE INDEX idx_punishments_identity ON punishments(identity_id, issued_at);
CREATE INDEX idx_early_drops_identity ON early_drop_attempts(identity_id, attempted_at);
"""


SCHEMA_V2 = """
CREATE TABLE punish_intents (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  case_id INTEGER,
  kind TEXT NOT NULL,
  level INTEGER,
  duration TEXT,
  reason TEXT,
  mode TEXT NOT NULL,
  status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

CREATE TABLE outbox (
  id INTEGER PRIMARY KEY,
  group_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);

CREATE INDEX idx_punish_intents_status ON punish_intents(status, created_at);
CREATE INDEX idx_outbox_status ON outbox(status, created_at);
"""

SCHEMA_V3 = """
CREATE TABLE drop_credits (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER NOT NULL,
  round_id INTEGER,
  drop_id INTEGER,
  granted_for TEXT,
  reason TEXT,
  consumed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE group_admins (
  group_key TEXT NOT NULL,
  tg_user_id INTEGER NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (group_key, tg_user_id)
);

CREATE INDEX idx_drop_credits_identity ON drop_credits(identity_id, consumed_at);
"""

SCHEMA_V4 = """
CREATE TABLE read_requests (
  id INTEGER PRIMARY KEY,
  tweet_id TEXT NOT NULL,
  identity_id INTEGER,
  reason TEXT,
  status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  done_at TEXT
);

CREATE INDEX idx_read_requests_status ON read_requests(status, created_at);

ALTER TABLE engagement_reads ADD COLUMN comment_text TEXT;
ALTER TABLE engagement_reads ADD COLUMN quality INTEGER DEFAULT 1;
ALTER TABLE engagement_reads ADD COLUMN tweet_position INTEGER;
"""

SCHEMA_V5 = """
ALTER TABLE drops ADD COLUMN comments_open INTEGER DEFAULT 1;
ALTER TABLE engagement_reads ADD COLUMN conversation_open INTEGER DEFAULT 1;
ALTER TABLE engagement_reads ADD COLUMN read_complete INTEGER DEFAULT 0;
"""

SCHEMA_VERSIONS = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3, 4: SCHEMA_V4, 5: SCHEMA_V5}
LATEST_SCHEMA_VERSION = max(SCHEMA_VERSIONS)


# A case stays "actioned" after a punishment so the member can still appeal it and an
# admin can still clear or confirm it. It must not be left "open", or the scheduler
# would escalate the same case forever, and it must not be closed, or the appeal and
# the card buttons would both be dead ends.
CASE_OPEN = "open"
CASE_ACTIONED = "actioned"
CASE_CLEARED = "cleared"
CASE_ADMIN_MANUAL = "admin_manual"
CASE_RESOLVED_BY_MEMBER = ("actioned", "admin_manual", "cleared")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] or 0
    for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
        conn.executescript(SCHEMA_VERSIONS[version])
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, now_iso()),
        )
        conn.commit()


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def q1(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def ex(conn, sql, args=()) -> int:
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid
