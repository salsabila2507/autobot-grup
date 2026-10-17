import os
from dataclasses import dataclass
from typing import Dict, List

import yaml

from engine.rounds import parse_schedule


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AppCfg:
    api_id: int
    api_hash: str


@dataclass(frozen=True)
class CircleCfg:
    key: str
    topic_id: int
    kind: str
    required: int
    entry_format: str
    max_drops_per_round: int


@dataclass(frozen=True)
class GroupCfg:
    key: str
    main_group: int
    mod_group: int
    admin_user_ids: List[int]
    mod_bot_username: str
    topics: Dict[str, int]
    circles: List[CircleCfg]

    def circle_by_topic(self, topic_id: int):
        for c in self.circles:
            if c.topic_id == topic_id:
                return c
        return None

    def circle_by_key(self, key: str):
        for c in self.circles:
            if c.key == key:
                return c
        return None


@dataclass(frozen=True)
class RulesCfg:
    obligation_required: int
    vip_drops_per_24h: int
    miss_threshold: int
    grace_minutes: int
    enforce_deadline_minutes: int
    like_sla_minutes: int
    public_warnings: bool
    warn_ladder: Dict[int, str]
    max_warnings: int


@dataclass(frozen=True)
class RoundsCfg:
    mode: str
    schedule_utc: List


@dataclass(frozen=True)
class EnforcementCfg:
    mode: str
    native_fallback: bool


@dataclass(frozen=True)
class SecretsCfg:
    queue_bot_token_file: str
    userbot_session_file: str
    cookie_dir: str


@dataclass(frozen=True)
class XReaderCfg:
    """Settings for the browser reply reader and the sampled checker.

    The old GraphQL fields (per_account_requests_per_15m, jitter_seconds, page_cap,
    reread_minutes, shadow) went away with the API path that could not authenticate.
    """

    sample_size: int = 2
    cookie_file: str = ""
    browser_max_scrolls: int = 60
    browser_idle_rounds: int = 4
    browser_pause: float = 1.0
    confirm_sample: bool = True


@dataclass(frozen=True)
class Config:
    app: AppCfg
    groups: List[GroupCfg]
    rules: RulesCfg
    rounds: RoundsCfg
    enforcement: EnforcementCfg
    secrets: SecretsCfg
    x_reader: XReaderCfg


def _require(mapping, key, ctx):
    if not isinstance(mapping, dict) or key not in mapping or mapping[key] is None:
        raise ConfigError(f"config missing key: {ctx}{key}")
    return mapping[key]


def _int(value, ctx):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"config value must be int: {ctx} -> {value!r}")
    return value


def _parse_circle(raw):
    key = raw.get("key")
    ctx = f"groups[].circles[{key}]."
    kind = _require(raw, "kind", ctx)
    if kind not in ("engagement", "likes", "repost", "bookmark"):
        raise ConfigError(f"{ctx}kind invalid: {kind}")
    entry_format = raw.get("entry_format", "slash")
    if entry_format not in ("slash", "two_line", "three"):
        raise ConfigError(f"{ctx}entry_format invalid: {entry_format}")
    return CircleCfg(
        key=key,
        topic_id=_int(_require(raw, "topic_id", ctx), f"{ctx}topic_id"),
        kind=kind,
        required=_int(_require(raw, "required", ctx), f"{ctx}required"),
        entry_format=entry_format,
        max_drops_per_round=_int(raw.get("max_drops_per_round", 0), f"{ctx}max_drops_per_round"),
    )


def _parse_group(raw):
    key = _require(raw, "key", "groups[].")
    ctx = f"groups[{key}]."
    topics = _require(raw, "topics", ctx)
    for t in ("prison", "warnings", "weak_reply", "rules"):
        if t not in topics:
            raise ConfigError(f"{ctx}topics missing: {t}")
    circles_raw = _require(raw, "circles", ctx)
    if not circles_raw:
        raise ConfigError(f"{ctx}circles empty")
    circles = [_parse_circle(c) for c in circles_raw]
    ckeys = [c.key for c in circles]
    tids = [c.topic_id for c in circles]
    if len(set(ckeys)) != len(ckeys):
        raise ConfigError(f"{ctx}circles duplicate keys")
    if len(set(tids)) != len(tids):
        raise ConfigError(f"{ctx}circles duplicate topic ids")
    admins = raw.get("admin_user_ids") or []
    for a in admins:
        _int(a, f"{ctx}admin_user_ids")
    return GroupCfg(
        key=key,
        main_group=_int(_require(raw, "main_group", ctx), f"{ctx}main_group"),
        mod_group=_int(raw.get("mod_group", 0), f"{ctx}mod_group"),
        admin_user_ids=list(admins),
        mod_bot_username=raw.get("mod_bot_username", "MissRose_bot"),
        topics=dict(topics),
        circles=circles,
    )


def load(path: str) -> Config:
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    app_raw = _require(raw, "app", "")
    app = AppCfg(
        api_id=_int(_require(app_raw, "api_id", "app."), "app.api_id"),
        api_hash=str(_require(app_raw, "api_hash", "app.")),
    )
    groups_raw = _require(raw, "groups", "")
    if not groups_raw:
        raise ConfigError("groups empty")
    groups = [_parse_group(g) for g in groups_raw]
    gkeys = [g.key for g in groups]
    if len(set(gkeys)) != len(gkeys):
        raise ConfigError("duplicate group keys")
    rules_raw = _require(raw, "rules", "")
    ladder_raw = dict(_require(rules_raw, "warn_ladder", "rules."))
    rules = RulesCfg(
        obligation_required=_int(_require(rules_raw, "obligation_required", "rules."), "rules.obligation_required"),
        vip_drops_per_24h=_int(_require(rules_raw, "vip_drops_per_24h", "rules."), "rules.vip_drops_per_24h"),
        miss_threshold=_int(_require(rules_raw, "miss_threshold", "rules."), "rules.miss_threshold"),
        grace_minutes=_int(_require(rules_raw, "grace_minutes", "rules."), "rules.grace_minutes"),
        enforce_deadline_minutes=_int(_require(rules_raw, "enforce_deadline_minutes", "rules."), "rules.enforce_deadline_minutes"),
        like_sla_minutes=_int(_require(rules_raw, "like_sla_minutes", "rules."), "rules.like_sla_minutes"),
        public_warnings=bool(_require(rules_raw, "public_warnings", "rules.")),
        warn_ladder={int(k): str(v) for k, v in ladder_raw.items()},
        max_warnings=_int(_require(rules_raw, "max_warnings", "rules."), "rules.max_warnings"),
    )
    rounds_raw = _require(raw, "rounds", "")
    mode = _require(rounds_raw, "mode", "rounds.")
    if mode != "fixed":
        raise ConfigError(f"rounds.mode must be fixed, got: {mode}")
    schedule = _require(rounds_raw, "schedule_utc", "rounds.")
    try:
        parse_schedule(schedule)
    except ValueError as e:
        raise ConfigError(str(e))
    enforcement_raw = _require(raw, "enforcement", "")
    emode = _require(enforcement_raw, "mode", "enforcement.")
    if emode not in ("list_only", "list_then_auto", "auto"):
        raise ConfigError(f"enforcement.mode invalid: {emode}")
    enforcement = EnforcementCfg(mode=emode, native_fallback=bool(enforcement_raw.get("native_fallback", False)))
    secrets_raw = _require(raw, "secrets", "")
    secrets = SecretsCfg(
        queue_bot_token_file=_require(secrets_raw, "queue_bot_token_file", "secrets."),
        userbot_session_file=_require(secrets_raw, "userbot_session_file", "secrets."),
        cookie_dir=_require(secrets_raw, "cookie_dir", "secrets."),
    )
    xr = _require(raw, "x_reader", "")
    x_reader = XReaderCfg(
        sample_size=_int(xr.get("sample_size", 2), "x_reader.sample_size"),
        cookie_file=str(xr.get("cookie_file", "")),
        browser_max_scrolls=_int(xr.get("browser_max_scrolls", 60), "x_reader.browser_max_scrolls"),
        browser_idle_rounds=_int(xr.get("browser_idle_rounds", 4), "x_reader.browser_idle_rounds"),
        browser_pause=float(xr.get("browser_pause", 1.0)),
        confirm_sample=bool(xr.get("confirm_sample", True)),
    )
    return Config(
        app=app,
        groups=groups,
        rules=rules,
        rounds=RoundsCfg(mode=mode, schedule_utc=list(schedule)),
        enforcement=enforcement,
        secrets=secrets,
        x_reader=x_reader,
    )


def read_secret(cfg: Config, name: str) -> str:
    files = {
        "queue_bot_token": cfg.secrets.queue_bot_token_file,
        "userbot_session": cfg.secrets.userbot_session_file,
    }
    if name not in files:
        raise ConfigError(f"unknown secret: {name}")
    path = files[name]
    if not os.path.isfile(path):
        raise ConfigError(f"secret file not found: {path}")
    if os.stat(path).st_mode & 0o077:
        raise ConfigError(f"secret file too open, chmod 600 required: {path}")
    with open(path, "r", encoding="utf-8") as f:
        value = f.read().strip()
    if not value:
        raise ConfigError(f"secret file empty: {path}")
    return value
