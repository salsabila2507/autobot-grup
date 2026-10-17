from datetime import timedelta

from engine import db as dbm
from engine import identity as idm
from engine import numbering
from tests.helpers import connect, circle, dt, iso


def test_parse_valid_link():
    handle, tweet_id = numbering.parse_x_link("https://x.com/someone/status/1234567890123456789")
    assert handle == "someone"
    assert tweet_id == "1234567890123456789"


def test_parse_link_with_query_and_mobile_host():
    handle, tweet_id = numbering.parse_x_link("https://mobile.twitter.com/SomeUser/status/99?s=20")
    assert handle == "someuser"
    assert tweet_id == "99"


def test_reject_internal_status_link():
    assert numbering.link_reject_reason("https://x.com/i/status/1234567890") == "internal_status"


def test_reject_profile_only_link():
    assert numbering.link_reject_reason("https://x.com/someone") == "profile_only"


def test_reject_non_x_link():
    assert numbering.link_reject_reason("https://facebook.com/x/status/1") == "not_x"


def test_reject_plain_text():
    assert numbering.link_reject_reason("hello world") == "bad_format"


def test_accept_valid_link():
    assert numbering.link_reject_reason("https://x.com/a/status/5555555") is None


def test_positions_increment(tmp_path):
    conn = connect(tmp_path)
    assert numbering.next_position(conn, "eng_blue") == 1
    conn.execute("INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("g", 1, "eng_blue", 1, 1, 1, "l", "1", iso(dt(2026, 1, 1))))
    conn.commit()
    assert numbering.next_position(conn, "eng_blue") == 2


def test_deleted_entry_leaves_gap(tmp_path):
    conn = connect(tmp_path)
    for pos in (1, 2, 3):
        conn.execute("INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("g", 1, "eng_blue", pos, 1, 1, "l", str(pos), "deleted" if pos == 2 else "active", iso(dt(2026, 1, 1))))
    conn.commit()
    assert numbering.next_position(conn, "eng_blue") == 4


def test_render_entry_regular(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 111, "alice", "alice_x")
    rendered = numbering.render_entry(circle(), 1, ident, "https://x.com/alice_x/status/42")
    assert rendered == "#1 / @alice alice_x / https://x.com/alice_x/status/42"


def test_render_entry_vip(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 111, "alice", "alice_x")
    idm.set_vip(conn, 111, True)
    ident = idm.resolve_identity(conn, 111)
    rendered = numbering.render_entry(circle(), 3, ident, "https://x.com/alice_x/status/42")
    assert rendered == "#3 / VIP ★ / https://x.com/alice_x/status/42"


def test_render_entry_two_line(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 222, "bob", "bob_x")
    rendered = numbering.render_entry(circle(entry_format="two_line"), 7, ident, "https://x.com/bob_x/status/1")
    assert rendered == "#7 @bob bob_x\nhttps://x.com/bob_x/status/1"


def test_display_link_likes_circle_keeps_the_real_post_url(tmp_path):
    """The intent link answers 307, which leaves Telegram's preview blank."""
    link = numbering.display_link(circle(kind="likes"), "https://x.com/a/status/5", "5")
    assert link == "x.com/a/status/5"


def test_display_link_engagement_passthrough():
    link = numbering.display_link(circle(), "https://x.com/a/status/5", "5")
    assert link == "x.com/a/status/5"


def test_status_without_id_rejected():
    assert numbering.parse_x_link("https://x.com/username/status") is None
    assert numbering.link_reject_reason("https://x.com/username/status") == "bad_format"


def test_status_with_trailing_slash_only_rejected():
    assert numbering.parse_x_link("https://x.com/username/status/") is None
    assert numbering.link_reject_reason("https://x.com/username/status/") == "bad_format"


def test_status_with_nonnumeric_id_rejected():
    assert numbering.parse_x_link("https://x.com/username/status/abc") is None
    assert numbering.link_reject_reason("https://x.com/username/status/abc") == "bad_format"


def test_profile_only_rejected():
    assert numbering.parse_x_link("https://x.com/username") is None
    assert numbering.link_reject_reason("https://x.com/username") == "profile_only"


def test_internal_status_rejected():
    assert numbering.parse_x_link("https://x.com/i/status/1234567890") is None
    assert numbering.link_reject_reason("https://x.com/i/status/1234567890") == "internal_status"


def test_valid_status_accepted_with_query_string():
    handle, tid = numbering.parse_x_link("https://x.com/username/status/1234567890?s=20")
    assert handle == "username"
    assert tid == "1234567890"


def test_handle_is_lowercased():
    handle, _ = numbering.parse_x_link("https://x.com/MixedCaseHandle/status/1234567890")
    assert handle == "mixedcasehandle"


def test_reject_reason_none_means_accepted():
    assert numbering.link_reject_reason("https://x.com/username/status/1234567890") is None








def test_scheme_is_stripped_from_the_shown_link():
    shown = numbering.display_link(
        circle(kind="engagement"),
        "https://x.com/member1/status/1111111876543234567890",
        "1111111876543234567890",
    )
    assert shown == "x.com/member1/status/1111111876543234567890"


def test_www_and_http_forms_are_normalised():
    for raw in (
        "http://x.com/member1/status/1111111876543234567890",
        "https://www.x.com/member1/status/1111111876543234567890",
        "x.com/member1/status/1111111876543234567890",
        "  x.com/member1/status/1111111876543234567890/  ",
    ):
        assert numbering.normalize_link(raw) == "x.com/member1/status/1111111876543234567890"


def test_old_twitter_hosts_fold_into_x_com():
    for host in ("twitter.com", "mobile.twitter.com", "www.twitter.com"):
        raw = f"https://{host}/member1/status/1111111876543234567890"
        assert numbering.normalize_link(raw) == "x.com/member1/status/1111111876543234567890"




def test_display_link_engagement_passthrough():
    original = "https://x.com/u/status/1234567890"
    assert numbering.display_link(circle(kind="engagement"), original, "1234567890") == \
        "x.com/u/status/1234567890"


def test_likes_circle_entry_uses_rewritten_link(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 111, "alice", "alice_x")
    like_circle = circle(key="like20", topic_id=6, kind="likes", required=20)
    rendered = numbering.render_entry(like_circle, 4, ident, "https://x.com/intent/like?tweet_id=1234567890")
    assert rendered == "#4 / @alice alice_x / https://x.com/intent/like?tweet_id=1234567890"




# --- the number must survive on every entry, badge or not -------------

def _ident(conn, tg, username, handle, vip=False):
    ident, _ = idm.bind_handle(conn, tg, username, handle)
    if vip:
        conn.execute("UPDATE identities SET vip=1 WHERE id=?", (ident.id,))
        conn.commit()
        ident = idm.identity_by_id(conn, ident.id)
    return ident


def test_every_three_line_entry_shows_its_number(tmp_path):
    """Without the number nobody can point at an entry, and /del works by number."""
    from engine.models import Identity

    L = "x.com/bakioption/status/2110301246036824174"
    c = circle(entry_format="three")

    def fake(tg, handle, vip=False):
        return Identity(id=tg, x_handle=handle, vip=vip)

    assert numbering.render_entry(c, 56, fake(1, "koyum_1"), L).startswith("★56")
    assert numbering.render_entry(c, 57, fake(2, "jexbr", vip=True), L,
                                  is_admin=True).startswith("★57")
    assert numbering.render_entry(c, 58, fake(3, "vipboss_x", vip=True), L).startswith("★58")
    assert numbering.render_entry(c, 59, fake(4, "boss_x"), L,
                                  is_admin=True).startswith("★59")


def test_admin_badge_keeps_the_star_prefix_order():
    from engine import text

    out = text.entry_admin("three", 57, "jex", "jexbr", "x.com/a/status/1")
    assert out.splitlines()[0] == "★57 Admin ★"


def test_admin_and_vip_entry_shows_both_badges():
    from engine import text

    out = text.entry_admin("three", 59, "jex", "jexbr", "x.com/a/status/1", vip=True)
    assert out.splitlines()[0] == "★59 Admin ★ Vip ★"


def test_vip_entry_keeps_the_number():
    from engine import text

    out = text.entry_vip("three", 58, "x.com/a/status/1")
    assert out.splitlines()[0] == "★58 Vip ★"


def test_badge_entries_leave_the_second_line_blank():
    from engine import text

    out = text.entry_admin("three", 57, "jex", "jexbr", "x.com/a/status/1")
    lines = out.splitlines()
    assert lines[1] == ""
    assert lines[2] == "x.com/a/status/1"


# --- members type x.com/... without a scheme ---------------------------

def test_display_link_keeps_the_intent_scheme_out_of_the_entry():
    """The posted link stays the canonical one: the intent/like form answers with a 307
    and leaves Telegram's preview blank."""
    shown = numbering.display_link(circle(kind="likes"),
                                   "https://x.com/a/status/123", "123")
    assert shown == "x.com/a/status/123"
    assert "intent" not in shown


def test_entries_carry_no_button_at_all():
    """Owner asked for the like-circle button to go: it muddied the entry."""
    import inspect
    from services import queue_bot as qb

    src = inspect.getsource(qb.QueueBot.send_entry)
    assert "buttons" not in src, "send_entry still accepts buttons"


def test_scheme_is_optional_in_dropped_links():
    """Telegram auto-links "x.com/a/status/1", and that is the form we print ourselves."""
    want = ("shakib_350", "2110327811885940883")
    for raw in (
        "x.com/Shakib_350/status/2110327811885940883",
        "https://x.com/Shakib_350/status/2110327811885940883",
        "http://x.com/Shakib_350/status/2110327811885940883",
        "x.com/Shakib_350/status/2110327811885940883?s=20",
        "www.x.com/Shakib_350/status/2110327811885940883",
        "  x.com/Shakib_350/status/2110327811885940883  ",
    ):
        assert numbering.parse_x_link(raw) == want, raw
        assert numbering.link_reject_reason(raw) is None, raw


def test_schemeless_reject_reasons_stay_accurate():
    """Without a scheme urlparse dumps the host into path and everything reads 'not_x'."""
    assert numbering.link_reject_reason("x.com/Shakib_350") == "profile_only"
    assert numbering.link_reject_reason("x.com/i/status/123") == "internal_status"
    assert numbering.link_reject_reason("x.com/Shakib_350/status") == "bad_format"
    assert numbering.link_reject_reason("hello world") == "bad_format"


def test_schemeless_bad_segments_still_rejected():
    for raw in ("x.com/i/status/1", "x.com/explore", "x.com/intent/like?tweet_id=1"):
        assert numbering.parse_x_link(raw) is None, raw


# --- Telegram needs real Button objects, not dicts --------------------







