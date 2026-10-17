from datetime import datetime, timezone

from engine.config import CircleCfg, RulesCfg
from engine import db as dbm

UTC = timezone.utc


def connect(tmp_path):
    return dbm.connect(str(tmp_path / "engine.sqlite"))


def circle(key="eng_blue", topic_id=4, kind="engagement", required=30, entry_format="slash",
          max_drops_per_round=0):
    return CircleCfg(
        key=key,
        topic_id=topic_id,
        kind=kind,
        required=required,
        entry_format=entry_format,
        max_drops_per_round=max_drops_per_round,
    )


def rules(**over):
    base = dict(
        obligation_required=30,
        vip_drops_per_24h=4,
        miss_threshold=2,
        grace_minutes=5,
        enforce_deadline_minutes=15,
        like_sla_minutes=10,
        public_warnings=True,
        warn_ladder={1: "4h", 2: "48h", 3: "7d"},
        max_warnings=3,
    )
    base.update(over)
    return RulesCfg(**base)


def dt(*args, **kw) -> datetime:
    return datetime(*args, tzinfo=UTC, **kw)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def config(cfg_path, **over):
    import yaml
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw.update(over)
    return raw


def write_config(path, **over):
    import yaml
    raw = {
        "app": {"api_id": 111, "api_hash": "x"},
        "groups": [
            {
                "key": "hotti",
                "main_group": -1001,
                "mod_group": -2002,
                "admin_user_ids": [5],
                "mod_bot_username": "MissRose_bot",
                "topics": {"prison": 16, "warnings": 16, "weak_reply": 17, "rules": 18},
                "circles": [
                    {"key": "eng_blue", "topic_id": 4, "kind": "engagement", "required": 30, "entry_format": "slash", "max_drops_per_round": 0}
                ],
            }
        ],
        "rules": {
            "obligation_required": 30,
            "vip_drops_per_24h": 4,
            "miss_threshold": 2,
            "grace_minutes": 5,
            "enforce_deadline_minutes": 15,
            "like_sla_minutes": 10,
            "public_warnings": True,
            "warn_ladder": {1: "4h", 2: "48h", 3: "7d"},
            "max_warnings": 3,
        },
        "rounds": {"mode": "fixed", "schedule_utc": ["06:00-18:00", "18:00-06:00"]},
        "enforcement": {"mode": "list_only", "native_fallback": False},
        "secrets": {
            "queue_bot_token_file": "/tmp/nope_token",
            "userbot_session_file": "/tmp/nope_session",
            "cookie_dir": "/tmp/nope_cookies",
        },
        "x_reader": {
            "cookie_file": "/tmp/nope_cookie_header",
            "sample_size": 2,
            "confirm_sample": True,
            "browser_max_scrolls": 30,
            "browser_idle_rounds": 4,
            "browser_pause": 1.0,
        },
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f)
    return path
