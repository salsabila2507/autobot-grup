from dataclasses import dataclass
from typing import Optional


@dataclass
class Identity:
    id: int
    x_handle: str
    x_user_id: Optional[str] = None
    vip: bool = False
    status: str = "active"
    strikes: int = 0
    blacklisted_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    tg_username: Optional[str] = None


@dataclass
class Account:
    id: int
    identity_id: Optional[int]
    tg_user_id: int
    tg_username: Optional[str] = None
    joined_at: Optional[str] = None
    invite_link: Optional[str] = None
    status: str = "active"


@dataclass
class Round:
    id: int
    circle_key: str
    seq: int
    opened_at: str
    closed_at: Optional[str] = None


@dataclass
class GateResult:
    ok: bool
    reason: Optional[str]
    identity: Optional[Identity]
    account: Optional[Account]
