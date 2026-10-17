import re
from urllib.parse import urlparse

from engine import db as dbm
from engine import text
from engine.config import CircleCfg
from engine.models import Identity

_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_BAD_FIRST_SEGMENTS = {
    "i", "intent", "search", "explore", "home", "messages", "notifications",
    "settings", "compose", "hashtag", "personalization",
}
# Telegram turns a bare "x.com/user/status/1" into a link by itself, and the entry we
# post is written in exactly that form. Requiring "https://" meant members had to type
# something nobody types by hand just to drop a link, so the scheme is optional here.
_LINK_RE = re.compile(
    r"(?:https?://)?(?:(?:www|mobile|m)\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{1,25})(?:[/?#]\S*)?",
    re.IGNORECASE,
)
_ANY_URL_RE = re.compile(r"(?:https?://)?(?:www\.|mobile\.|m\.)?(?:x|twitter)\.com/\S+", re.IGNORECASE)
_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def parse_x_link(value: str):
    m = _LINK_RE.search(value or "")
    if not m:
        return None
    handle = m.group(1).lower()
    if handle in _BAD_FIRST_SEGMENTS:
        return None
    return handle, m.group(2)


def link_reject_reason(value: str):
    if parse_x_link(value) is not None:
        return None
    m = _ANY_URL_RE.search(value or "")
    if not m:
        # Nothing that looks like an X address. If it is a link to somewhere else, say
        # so, otherwise the member just gets "bad format" and learns nothing.
        generic = _GENERIC_URL_RE.search(value or "")
        if generic:
            return "not_x"
        return "bad_format"
    raw = m.group(0)
    if not raw.lower().startswith("http"):
        # urlparse puts a scheme-less "x.com/a/b" entirely into path, so give it a
        # scheme first or the reason comes back as a misleading "not_x".
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "bad_format"
    host = parsed.netloc.lower()
    segments = [s for s in parsed.path.split("/") if s]
    if host in _HOSTS:
        if len(segments) >= 2 and segments[0] == "i" and segments[1] == "status":
            return "internal_status"
        if segments and segments[0] in _BAD_FIRST_SEGMENTS:
            return "bad_format"
        if "status" not in segments:
            return "profile_only"
        return "bad_format"
    return "not_x"


def next_position(conn, circle_key: str) -> int:
    row = dbm.q1(conn, "SELECT COALESCE(MAX(position), 0) AS m FROM drops WHERE circle_key=?", (circle_key,))
    return int(row["m"]) + 1


def normalize_link(link: str, tweet_id: str | None = None) -> str:
    """Rewrite a posted X link into the bare x.com form members recognise.

    https://x.com/member1/status/1111111876543234567890
      -> x.com/member1/status/1111111876543234567890

    Older twitter.com and mobile.twitter.com addresses are folded into x.com as well,
    because Telegram renders the preview of the old host inconsistently and a member
    seeing a broken preview reads it as a dead link.
    """
    value = (link or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    if value.lower().startswith("www."):
        value = value[4:]
    for host in ("mobile.twitter.com", "mobile.x.com", "twitter.com", "www.twitter.com"):
        if value.lower().startswith(host.lower() + "/"):
            value = "x.com/" + value[len(host) + 1:]
            break
    value = value.rstrip("/")
    return value


def display_link(circle: CircleCfg, link: str, tweet_id: str) -> str:
    """The URL shown in the entry.

    It always stays the canonical post address, in the bare x.com form. The intent links
    reply with a 307 redirect, which leaves Telegram's link preview blank, and a dead
    preview looks like a dead link to members.
    """
    return normalize_link(link, tweet_id)


def render_entry(circle: CircleCfg, position: int, identity: Identity, link: str,
                 is_admin: bool = False) -> str:
    if is_admin:
        tg = identity.tg_username or str(identity.id)
        return text.entry_admin(circle.entry_format, position, tg,
                               identity.x_handle, link, vip=identity.vip)
    if identity.vip:
        return text.entry_vip(circle.entry_format, position, link)
    tg = identity.tg_username or str(identity.id)
    return text.entry(circle.entry_format, position, tg, identity.x_handle, link)
