from engine import db as dbm


def rules_link(group) -> str:
    return f"https://t.me/c/{str(group.main_group).removeprefix('-100')}/{group.topics['rules']}"


def defaulter_card(case, identity, drop, missing_tweets, group_key: str):
    lines = [
        f"Defaulter #{case.id}",
        f"Identity: @{identity.x_handle}" if identity else "Identity: unknown",
        f"Drop: {drop['circle_key']} #{drop['position']} round {drop['round_id']} ({group_key})",
        f"Missing {case.missing_count}: {', '.join(missing_tweets[:10])}",
        "Evidence: x_reader",
    ]
    text_value = "\n".join(lines)
    keyboard = [
        [
            {"text": "Warn", "callback": f"case:{case.id}:warn"},
            {"text": "Clear", "callback": f"case:{case.id}:clear"},
        ]
    ]
    return text_value, keyboard


def apply_callback(conn, query_data: str, admin_tg_user_id: int, group_admin_ids: list[int]):
    if admin_tg_user_id not in group_admin_ids:
        return {"ok": False, "reason": "admins only"}
    parts = query_data.split(":")
    if len(parts) != 3 or parts[0] != "case":
        return {"ok": False, "reason": "bad callback"}
    _, case_id, verb = parts
    row = dbm.q1(conn, "SELECT * FROM cases WHERE id=?", (int(case_id),))
    if row is None or row["status"] not in dbm.CASE_RESOLVED_BY_MEMBER:
        return {"ok": False, "reason": f"case {case_id} not open"}
    if verb == "clear":
        dbm.ex(
            conn,
            "UPDATE cases SET status='cleared', closed_at=?, closed_by=?, close_reason='admin_clear' WHERE id=?",
            (dbm.now_iso(), admin_tg_user_id, row["id"]),
        )
        return {"ok": True, "action": "clear"}
    if verb == "warn":
        dbm.ex(
            conn,
            "UPDATE cases SET status='admin_manual', closed_at=?, closed_by=?, close_reason='admin_warn' WHERE id=?",
            (dbm.now_iso(), admin_tg_user_id, row["id"]),
        )
        return {"ok": True, "action": "warn"}
    return {"ok": False, "reason": "unknown verb"}
