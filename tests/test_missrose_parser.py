from engine import db as dbm
from engine import missrose
from tests.helpers import connect, dt, iso


def test_warn_line():
    a = missrose.parse_line("Jex has been warned (1/3 warns)")
    assert a is not None
    assert a.kind == "warn"
    assert a.level == 1


def test_warn_line_second_level():
    a = missrose.parse_line("someone has been warned (2/3 warns)")
    assert a.kind == "warn"
    assert a.level == 2


def test_mute_from_warning_line():
    a = missrose.parse_line("That's 2/3 warnings; Jex is muted for 1 hours")
    assert a.kind == "mute"
    assert a.level == 2
    assert a.duration == "1 hours"


def test_mute_days():
    a = missrose.parse_line("Muted Jex for 2 days")
    assert a.kind == "mute"
    assert a.duration == "2 days"


def test_mute_hours():
    a = missrose.parse_line("Muted Jex for 4 hours")
    assert a.kind == "mute"
    assert a.duration == "4 hours"


def test_shhh_mute():
    a = missrose.parse_line("Shhh... quiet now. Muted Jex for 2 days")
    assert a.kind == "mute"
    assert a.duration == "2 days"


def test_ban_line():
    a = missrose.parse_line("Banned Jex")
    assert a.kind == "ban"


def test_ban_line_with_period():
    a = missrose.parse_line("Banned Jex.")
    assert a.kind == "ban"


def test_shunned_line():
    a = missrose.parse_line("Shunned Jex")
    assert a.kind == "ban"


def test_username_extracted():
    a = missrose.parse_line("Jex has been warned (1/3 warns)")
    assert a.target_username == "Jex"


def test_username_from_at_form():
    a = missrose.parse_line("@Jexeth017 has been warned (3/3 warns)")
    assert a.target_username == "Jexeth017"
    assert a.level == 3


def test_unknown_line_returns_none():
    assert missrose.parse_line("Some random chatter in the group") is None


def test_empty_line_returns_none():
    assert missrose.parse_line("") is None


def test_ingest_parsed_row(tmp_path):
    conn = connect(tmp_path)
    eid = missrose.ingest(conn, "Jex has been warned (1/3 warns)")
    row = dbm.q1(conn, "SELECT * FROM external_actions WHERE id=?", (eid,))
    assert row["status"] == "parsed"
    assert row["kind"] == "warn"
    assert row["level"] == 1
    assert row["source_bot"] == "MissRose_bot"
    assert row["observed_at"]


def test_ingest_unparsed_row(tmp_path):
    conn = connect(tmp_path)
    before = dbm.q1(conn, "SELECT COUNT(*) AS n FROM identities")["n"]
    eid = missrose.ingest(conn, "totally unknown line")
    row = dbm.q1(conn, "SELECT * FROM external_actions WHERE id=?", (eid,))
    assert row["status"] == "unparsed"
    assert row["kind"] is None
    after = dbm.q1(conn, "SELECT COUNT(*) AS n FROM identities")["n"]
    assert before == after


def test_ingest_unknown_line_does_not_move_counter(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO identities (x_handle, strikes, first_seen_at) VALUES (?,?,?)",
        ("jex", 1, iso(dt(2026, 1, 1))),
    )
    conn.execute(
        "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)",
        (1, 111, "Jex"),
    )
    conn.commit()

    missrose.ingest(conn, "Jex has been warned (2/3 warns)")
    before = dbm.q1(conn, "SELECT strikes FROM identities WHERE id=1")["strikes"]
    assert before == 2

    missrose.ingest(conn, "a line nobody can parse 12345")
    after = dbm.q1(conn, "SELECT strikes FROM identities WHERE id=1")["strikes"]
    assert after == before


def test_ingest_reconciles_strikes_upward(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO identities (x_handle, strikes, first_seen_at) VALUES (?,?,?)",
        ("jex", 2, iso(dt(2026, 1, 1))),
    )
    conn.execute(
        "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)",
        (1, 111, "Jex"),
    )
    conn.commit()

    missrose.ingest(conn, "Jex has been warned (3/3 warns)")
    strikes = dbm.q1(conn, "SELECT strikes FROM identities WHERE id=1")["strikes"]
    assert strikes == 3


def test_ingest_never_lowers_strikes(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO identities (x_handle, strikes, first_seen_at) VALUES (?,?,?)",
        ("jex", 3, iso(dt(2026, 1, 1))),
    )
    conn.execute(
        "INSERT INTO accounts (identity_id, tg_user_id, tg_username) VALUES (?,?,?)",
        (1, 111, "Jex"),
    )
    conn.commit()

    missrose.ingest(conn, "Jex has been warned (1/3 warns)")
    strikes = dbm.q1(conn, "SELECT strikes FROM identities WHERE id=1")["strikes"]
    assert strikes == 3


def test_ingest_unmatched_target_has_no_identity(tmp_path):
    conn = connect(tmp_path)
    eid = missrose.ingest(conn, "GhostUser has been warned (1/3 warns)")
    row = dbm.q1(conn, "SELECT * FROM external_actions WHERE id=?", (eid,))
    assert row["status"] == "parsed"
    assert row["target_tg_user_id"] is None
