from engine import db as dbm
from engine import identity as idm
from engine import outbox
from tests.helpers import connect, dt, iso
from services import userbot


def _identity(conn, tg_id=111, handle="alice_x", username="alice"):
    ident, _ = idm.bind_handle(conn, tg_id, username, handle)
    return ident


def _intent(conn, ident, kind="warn", level=1, duration="4h", reason="missed engagements", mode="bot_auto", case_id=None):
    return outbox.queue_punish(
        conn,
        ident.id,
        case_id,
        {"kind": kind, "level": level, "duration": duration, "reason": reason, "mode": mode},
    )


def test_resolve_target_uses_primary_active_account(tmp_path):
    conn = connect(tmp_path)
    _identity(conn)
    row = dbm.q1(conn, "SELECT * FROM accounts WHERE tg_user_id=111")
    assert userbot.resolve_target(conn, row["identity_id"]) == "alice"


def test_resolve_target_none_when_no_username(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 222, None, "bob_x")
    assert userbot.resolve_target(conn, ident.id) is None


def test_resolve_target_skips_banned_account(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    dbm.ex(conn, "UPDATE accounts SET status='banned' WHERE tg_user_id=111")
    assert userbot.resolve_target(conn, ident.id) is None


def test_build_warn_command(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident)
    row = outbox.pending_punish(conn)[0]
    cmd = userbot.build_command(conn, row)
    assert cmd.startswith("/warn @alice")
    assert "missed engagements" in cmd


def test_build_mute_command_carries_duration(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident, kind="mute", duration="48h")
    row = outbox.pending_punish(conn)[0]
    assert userbot.build_command(conn, row) == "/mute @alice 48h"


def test_build_ban_command(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident, kind="ban", reason="alt abuse")
    row = outbox.pending_punish(conn)[0]
    assert userbot.build_command(conn, row) == "/ban @alice alt abuse"


def test_build_command_none_for_unresolvable_target(tmp_path):
    conn = connect(tmp_path)
    ident, _ = idm.bind_handle(conn, 333, None, "ghost_x")
    _intent(conn, ident)
    row = outbox.pending_punish(conn)[0]
    assert userbot.build_command(conn, row) is None


def test_build_command_none_for_unknown_kind(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident, kind="banlist")
    row = outbox.pending_punish(conn)[0]
    assert userbot.build_command(conn, row) is None


def test_list_only_never_returns_intents(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident)
    assert userbot.intents_for(conn, "list_only") == []
    assert len(outbox.pending_punish(conn)) == 1, "list_only keeps them queued"


def test_auto_mode_returns_intents(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident)
    assert len(userbot.intents_for(conn, "auto")) == 1


def test_list_then_auto_returns_intents(tmp_path):
    conn = connect(tmp_path)
    ident = _identity(conn)
    _intent(conn, ident)
    assert len(userbot.intents_for(conn, "list_then_auto")) == 1


def test_heartbeat_written(tmp_path):
    conn = connect(tmp_path)
    userbot.heartbeat(conn)
    row = dbm.q1(conn, "SELECT value FROM kv WHERE key='hb:userbot'")
    assert row is not None
    assert row["value"].endswith("+00:00")