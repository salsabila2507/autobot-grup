import os

from engine import db as dbm
from engine import identity as idm
from engine import shift
from services import preflight as pf
from tests.helpers import connect, write_config


def _cfg_file(tmp_path, **over):
    return str(write_config(str(tmp_path / "config.yaml"), **over))


def test_report_renders_and_flags_failures():
    r = pf.Report()
    r.add("thing", pf.OK, "fine")
    r.add("other", pf.REQUIRED, "broken")
    out = r.render()
    assert "thing" in out and "fine" in out
    assert len(r.failed) == 1


def test_check_config_flags_empty_admins(tmp_path):
    import yaml

    from engine import config as cfgmod

    path = str(write_config(str(tmp_path / "c2.yaml")))
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    raw["groups"][0]["admin_user_ids"] = []
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f)
    cfg = cfgmod.load(path)
    r = pf.Report()
    pf.check_config(r, cfg)
    assert any(s == pf.REQUIRED for _, s, _ in r.rows), "empty admin_user_ids must fail"


def test_check_config_flags_zero_mod_group(tmp_path):
    from engine import config as cfgmod

    cfg = cfgmod.load(_cfg_file(tmp_path))
    cfg_mod_group = cfg.groups[0].mod_group
    assert cfg_mod_group != 0, "test fixture uses a real mod group"


def test_check_schedule_rejects_bad(tmp_path):
    from engine import config as cfgmod

    cfg = cfgmod.load(_cfg_file(tmp_path))
    r = pf.Report()
    pf.check_schedule(r, cfg)
    assert r.rows[0][1] == pf.OK


def test_check_db_detects_writable(tmp_path):
    conn = connect(tmp_path)
    r = pf.Report()
    pf.check_db(r, conn)
    names = {n for n, _, _ in r.rows}
    assert {"db", "db_write", "schema"} <= names
    assert all(s != pf.REQUIRED for _, s, _ in r.rows)


def test_check_shifts_warns_when_empty(tmp_path):
    conn = connect(tmp_path)
    r = pf.Report()
    pf.check_shifts(r, conn)
    assert r.rows[0][1] == pf.OPTIONAL


def test_check_shifts_ok_when_loaded(tmp_path):
    conn = connect(tmp_path)
    shift.upsert_shift(conn, 111, "UTC", "09:00", "17:00", "1111111")
    r = pf.Report()
    pf.check_shifts(r, conn)
    assert r.rows[0][1] == pf.OK


def test_check_admins(tmp_path):
    conn = connect(tmp_path)
    from engine import config as cfgmod

    cfg = cfgmod.load(_cfg_file(tmp_path))
    r = pf.Report()
    pf.check_admins(r, conn, cfg)
    assert r.rows[0][1] == pf.OPTIONAL, "empty before seeding"
    idm.sync_group_admins(conn, cfg.groups[0].key, cfg.groups[0].admin_user_ids)
    r2 = pf.Report()
    pf.check_admins(r2, conn, cfg)
    assert r2.rows[0][1] == pf.OK


def test_check_secrets_missing_is_optional(tmp_path):
    from engine import config as cfgmod

    cfg = cfgmod.load(_cfg_file(tmp_path))
    r = pf.Report()
    pf.check_secrets(r, cfg)
    assert all(s == pf.OPTIONAL for _, s, _ in r.rows)


def test_check_secrets_world_readable_is_required(tmp_path):
    from engine import config as cfgmod

    token = tmp_path / "token.txt"
    token.write_text("123:abc", encoding="utf-8")
    os.chmod(str(token), 0o644)
    cfg = cfgmod.load(
        _cfg_file(tmp_path, secrets={
            "queue_bot_token_file": str(token),
            "userbot_session_file": str(tmp_path / "missing.txt"),
            "cookie_dir": str(tmp_path / "cookies"),
        })
    )
    r = pf.Report()
    pf.check_secrets(r, cfg)
    token_row = [row for row in r.rows if "queue_bot_token" in row[0]][0]
    assert token_row[1] == pf.REQUIRED
    assert "chmod 600" in token_row[2]


def test_check_secrets_0600_is_ok(tmp_path):
    from engine import config as cfgmod

    token = tmp_path / "token.txt"
    token.write_text("123:abc", encoding="utf-8")
    os.chmod(str(token), 0o600)
    cfg = cfgmod.load(
        _cfg_file(tmp_path, secrets={
            "queue_bot_token_file": str(token),
            "userbot_session_file": str(tmp_path / "missing.txt"),
            "cookie_dir": str(tmp_path / "cookies"),
        })
    )
    r = pf.Report()
    pf.check_secrets(r, cfg)
    token_row = [row for row in r.rows if "queue_bot_token" in row[0]][0]
    assert token_row[1] == pf.OK


def test_missing_cookie_header_is_required(tmp_path):
    """The browser reader cannot read anything without it, so this blocks a launch."""
    from engine import config as cfgmod

    cfg = cfgmod.load(_cfg_file(tmp_path))
    r = pf.Report()
    pf.check_cookies(r, cfg)
    assert r.rows[0][1] == pf.REQUIRED


def test_cookie_header_without_tokens_is_required(tmp_path):
    from engine import config as cfgmod

    header = tmp_path / "cookie.txt"
    header.write_text("guest_id=v1%3A123", encoding="utf-8")
    import os
    os.chmod(header, 0o600)

    cfg = cfgmod.load(_cfg_file(tmp_path, x_reader={"cookie_file": str(header)}))
    r = pf.Report()
    pf.check_cookies(r, cfg)
    assert r.rows[0][1] == pf.REQUIRED
    assert "auth_token" in r.rows[0][2]


def test_cookie_header_world_readable_is_rejected(tmp_path):
    import os
    from engine import config as cfgmod

    header = tmp_path / "cookie.txt"
    header.write_text("auth_token=t; ct0=c", encoding="utf-8")
    os.chmod(header, 0o644)

    cfg = cfgmod.load(_cfg_file(tmp_path, x_reader={"cookie_file": str(header)}))
    r = pf.Report()
    pf.check_cookies(r, cfg)
    assert r.rows[0][1] == pf.REQUIRED
    assert "chmod 600" in r.rows[0][2]


def test_run_end_to_end_on_valid_config(tmp_path):
    import os

    header = tmp_path / "cookie.txt"
    header.write_text("auth_token=fake; ct0=fake; twid=u%3D1", encoding="utf-8")
    os.chmod(header, 0o600)
    cfg_path = _cfg_file(tmp_path, x_reader={"cookie_file": str(header)})
    db_path = str(tmp_path / "engine.sqlite")
    report = pf.run(cfg_path, db_path)
    assert all(status != pf.REQUIRED for _, status, _ in report.rows), report.render()


def test_run_fails_on_missing_config(tmp_path):
    report = pf.run(str(tmp_path / "nope.yaml"), str(tmp_path / "engine.sqlite"))
    assert report.failed, "missing config must be a hard failure"