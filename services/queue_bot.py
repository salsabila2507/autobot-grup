import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone

from telethon import TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import UpdateBotMessageReaction

from engine import config as cfgmod
from engine import db as dbm
from engine import engine as eng
from engine import identity as idm
from engine import lookup
from engine import missrose
from engine import numbering
from engine import outbox
from engine import punishment as pm
from engine import rounds as rounds_mod
from engine import sampling
from engine import shift
from engine import text
from engine import cards
from engine import check_limits

log = logging.getLogger("queue_bot")

GROUP_FLAG = "queue_bot_started"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_admin(group, user_id) -> bool:
    if not user_id:
        return False
    return int(user_id) in [int(a) for a in group.admin_user_ids]


def reject_reason(reason: str, rules) -> str:
    return {
        "bad_format": text.REJECT_BAD_LINK,
        "not_x": text.REJECT_BAD_LINK,
        "internal_status": text.REJECT_BAD_LINK,
        "profile_only": text.REJECT_BAD_LINK,
        "duplicate": text.REJECT_DUPLICATE,
        "vip_limit": text.reject_vip_limit(rules.vip_drops_per_24h),
        "quarantine": text.QUARANTINE_NOTE,
        "banned": text.REJECT_BANNED,
        "handle_taken": text.QUARANTINE_NOTE,
        "obligation_open": text.EARLY_DROP_ALERT.format(
            mention="", required=rules.obligation_required
        ),
    }.get(reason, text.REJECT_BAD_LINK)


def vip_limit_for(circle, rules) -> int:
    return circle.max_drops_per_round or rules.vip_drops_per_24h


def prepare_drop(conn, group, circle, rules, schedule, msg, handle, tweet_id, now) -> dict:
    """Pure decision path for one member message. Returns an intent dict, no side effects."""
    round_row = rounds_mod.current_round(conn, circle.key, schedule, now)
    admin_sender = is_admin(group, msg.sender_id)
    result = idm.gate_drop(
        conn,
        group.key,
        circle.key,
        round_row.id,
        msg.sender_id,
        getattr(msg.sender, "username", None),
        handle,
        tweet_id,
        circle.required,
        vip_limit_for(circle, rules),
        now,
        is_admin=admin_sender,
    )
    intent = {
        "ok": result.ok,
        "reason": result.reason,
        "group_key": group.key,
        "round_id": round_row.id,
        "identity": result.identity,
        "account": result.account,
        "position": None,
        "entry": None,
        "drop_id": None,
        "record_early": False,
        "is_admin": admin_sender,
    }
    if not result.ok:
        if result.reason == "obligation_open" and result.identity:
            intent["record_early"] = True
        return intent

    position = numbering.next_position(conn, circle.key)
    link = numbering.display_link(circle, f"https://x.com/{handle}/status/{tweet_id}", tweet_id)
    intent["position"] = position
    intent["entry"] = numbering.render_entry(circle, position, result.identity, link,
                                             is_admin=bool(intent.get("is_admin")))
    return intent


def commit_drop(conn, circle, intent, handle, tweet_id, now) -> dict:
    """Persist an approved drop. Returns the drop id and the credit it used.

    The number has to be reserved before the entry goes out, otherwise two members
    dropping at the same moment would both be handed the same position. Reserving it
    also means a failed send must undo it, which is what rollback_drop is for.
    """
    drop_id = dbm.ex(
        conn,
        "INSERT INTO drops (group_key, round_id, circle_key, position, identity_id, account_id, link, tweet_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            intent["group_key"],
            intent["round_id"],
            circle.key,
            intent["position"],
            intent["identity"].id,
            intent["account"].id,
            f"https://x.com/{handle}/status/{tweet_id}",
            tweet_id,
            now.isoformat(timespec="seconds"),
        ),
    )
    # Admins are never judged, so they never take on debt. Creating an obligation for
    # them made /cleared report dozens of debts for an admin who can never be punished.
    obligation_id = None
    if not intent.get("is_admin"):
        obligation_id = idm.create_obligation(conn, intent["identity"].id, drop_id, circle.required)
    credit_id = idm.consume_drop_credit(conn, intent["identity"].id)
    return {"drop_id": drop_id, "obligation_id": obligation_id, "credit_id": credit_id}


def rollback_drop(conn, committed: dict) -> None:
    """Undo a drop whose entry never made it to the group.

    The number is freed and the debt the member had not yet earned is removed, so a
    failure on our side costs them nothing. Leaving it in place quietly consumed a
    number and left an obligation attached to an entry nobody can see.
    """
    drop_id = (committed or {}).get("drop_id")
    if drop_id is None:
        return
    dbm.ex(conn, "DELETE FROM obligations WHERE drop_id=?", (drop_id,))
    credit_id = (committed or {}).get("credit_id")
    if credit_id:
        dbm.ex(conn, "UPDATE drop_credits SET consumed_at=NULL WHERE id=?",
               (committed["credit_id"],))
    dbm.ex(conn, "DELETE FROM drops WHERE id=?", (drop_id,))


def mark_drop_sent(conn, drop_id: int, msg_id: int):
    dbm.ex(conn, "UPDATE drops SET msg_id=? WHERE id=?", (msg_id, drop_id))


def delete_wrong_link(conn, circle_key: str, position: int, admin_id: int, reason: str):
    """Admin delete of a wrong entry. The number stays consumed. Returns the drop row or None."""
    drop = dbm.q1(
        conn,
        "SELECT * FROM drops WHERE circle_key=? AND position=? AND status='active'",
        (circle_key, position),
    )
    if drop is None:
        return None
    # 'actioned' counts too: a case that already produced a punishment is still live
    # for the member's appeal and for the admin's card, so the entry cannot be deleted
    # out from under it.
    open_case = dbm.q1(conn, "SELECT id FROM cases WHERE drop_id=? AND status IN (?,?)",
                       (drop["id"], dbm.CASE_OPEN, dbm.CASE_ACTIONED))
    if open_case:
        return {"blocked": "open_case", "drop": drop}
    dbm.ex(
        conn,
        "UPDATE drops SET status='deleted_wrong_link', deleted_by=?, deleted_reason=?, deleted_at=? WHERE id=?",
        (admin_id, reason or "unspecified", dbm.now_iso(), drop["id"]),
    )
    idm.grant_drop_credit(conn, drop["identity_id"], drop["round_id"], drop["id"], reason or "wrong_link")
    # read the row back: the copy above predates the update, so its deleted_reason was
    # still None and the note told the admin the reason had been lost.
    fresh = dbm.q1(conn, "SELECT * FROM drops WHERE id=?", (drop["id"],))
    return {"blocked": None, "drop": fresh}


def label_drop(conn, circle_key: str, position: int, label: str):
    """Mark an entry as wrong or as a defaulter.

    This used to look for status='unlabeled', a value nothing in the codebase ever
    writes, so /label could never work on any drop at all. Entries are created as
    'active', so that is what it now matches.
    """
    if label not in ("wrong", "defaulter"):
        return None
    drop = dbm.q1(
        conn,
        "SELECT * FROM drops WHERE circle_key=? AND position=? AND status IN ('active','unlabeled')",
        (circle_key, position),
    )
    if drop is None:
        return None
    dbm.ex(conn, "UPDATE drops SET status=? WHERE id=?", (f"deleted_{label}", drop["id"]))
    return drop




def _reaction_emoji(reaction) -> str:
    """Emoji text for a reaction, or a placeholder for a custom one.

    The wire format carries `ReactionEmoji` (a character) or `ReactionCustomEmoji`
    (a document id and no character at all).
    """
    emoticon = getattr(reaction, "emoticon", None)
    if emoticon:
        return emoticon
    document_id = getattr(reaction, "document_id", None)
    if document_id:
        return f"custom:{document_id}"
    inner = getattr(reaction, "reaction", None)
    if inner is not None:
        return _reaction_emoji(inner)
    return ""


def _identity_for_entry(conn, msg_id: int):
    """Whose entry sits at this message, for /cleared by reply."""
    row = dbm.q1(conn, "SELECT identity_id FROM drops WHERE msg_id=?", (msg_id,))
    if not row:
        return None
    return idm.identity_by_id(conn, row["identity_id"])


def store_reaction(conn, drop_msg_id: int, reactor_tg_user_id: int, emoji: str, now: datetime):
    """Record the member's current reaction to an entry.

    This mirrors reality rather than just adding rows: switching the heart to something
    else, or removing it, has to withdraw the credit. Keeping a stale heart would count
    someone who has un-reacted as engaged.
    """
    from engine import checker as checker_mod

    drop = dbm.q1(conn, "SELECT id FROM drops WHERE msg_id=?", (drop_msg_id,))
    if drop is None:
        return None

    if not checker_mod.is_allowed_emoji(emoji):
        dbm.ex(
            conn,
            "DELETE FROM reactions WHERE drop_id=? AND reactor_tg_user_id=?",
            (drop["id"], reactor_tg_user_id),
        )
        return None

    dbm.ex(
        conn,
        "INSERT INTO reactions (drop_id, reactor_tg_user_id, emoji, observed_at) VALUES (?,?,?,?) "
        "ON CONFLICT(drop_id, reactor_tg_user_id) DO UPDATE SET emoji=excluded.emoji, "
        "observed_at=excluded.observed_at",
        (drop["id"], reactor_tg_user_id, emoji, now.isoformat(timespec="seconds")),
    )
    return drop["id"]


def topic_id_of(msg):
    rt = getattr(msg, "reply_to", None)
    if rt is not None and getattr(rt, "forum_topic", False):
        return msg.reply_to_msg_id
    return None


def find_target(cfg, conn, group, msg):
    tid = topic_id_of(msg)
    if tid is None:
        return None
    circle = group.circle_by_topic(tid)
    if circle is None:
        return None
    return circle


def _primary_account_id(conn, identity_id):
    row = dbm.q1(conn, "SELECT id FROM accounts WHERE identity_id=? ORDER BY id LIMIT 1", (identity_id,))
    return row["id"] if row else None


class QueueBot:
    def __init__(self, cfg, conn, client):
        self.cfg = cfg
        self.conn = conn
        self.client = client
        self.groups = {g.key: g for g in cfg.groups}
        self.limits = check_limits.CheckLimiter()
        for g in cfg.groups:
            idm.sync_group_admins(conn, g.key, g.admin_user_ids)

    async def send_to_topic(self, chat_id, topic_id, text_value, buttons=None,
                            parse_mode=None, link_preview=False):
        """Post a message with link previews switched off.

        Telegram turns a bare x.com address into an embedded card, which floods the
        circle with previews nobody asked for. Everything we post is a short line meant
        to be read as text, so previews stay off.
        """
        try:
            return await self.client.send_message(
                chat_id, text_value, reply_to=topic_id, buttons=buttons,
                parse_mode=parse_mode, link_preview=link_preview,
            )
        except errors.FloodWaitError as exc:
            log.warning("floodwait %s, sleeping", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            return await self.client.send_message(
                chat_id, text_value, reply_to=topic_id, buttons=buttons,
                parse_mode=parse_mode, link_preview=link_preview,
            )

    @staticmethod
    def build_buttons(spec):
        """Turn plain button data into real Telethon inline buttons.

        Only the review card uses buttons. Telegram answers a raw dict with
        ReplyMarkupInvalidError, so this has to be real Button.url objects, wrapped
        one level per row.
        """
        if not spec:
            return None
        from telethon.tl.custom import Button
        return [[Button.url(item["text"], item["url"]) for item in row] for row in spec]

    async def send_entry(self, chat_id, topic_id, body):
        """Post the numbered entry.

        A failed send is propagated on purpose: the caller rolls the drop back, so a
        member never loses their link or their number to a problem on our side.
        """
        return await self.send_to_topic(chat_id, topic_id, body)

    REJECT_LINGER_SECONDS = 8

    async def notify(self, chat_id, topic_id, text_value, linger=None):
        """Post a notice then delete it. Reject reasons would otherwise pile up and
        look like spam in the circle topic, one line per bad drop."""
        linger = self.REJECT_LINGER_SECONDS if linger is None else linger
        sent = await self.send_to_topic(chat_id, topic_id, text_value)
        if linger <= 0:
            return sent
        try:
            await asyncio.sleep(linger)
            await self.client.delete_messages(chat_id, [sent.id])
        except Exception as exc:
            log.warning("notice cleanup failed id=%s err=%s", sent.id, type(exc).__name__)
        return sent

    async def handle_entry_message(self, event, group, circle, msg):
        if msg.sender_id is None:
            await self.delete_message(msg)
            await self.notify(group.main_group, circle.topic_id, text.REJECT_ANON)
            return
        parsed = numbering.parse_x_link(msg.message or "")
        now = utcnow()
        if parsed is None:
            reason = numbering.link_reject_reason(msg.message or "")
            await self.delete_message(msg)
            await self.notify(group.main_group, circle.topic_id, reject_reason(reason, self.cfg.rules))
            return
        handle, tweet_id = parsed
        intent = prepare_drop(self.conn, group, circle, self.cfg.rules, self.cfg.rounds.schedule_utc, msg, handle, tweet_id, now)
        if not intent["ok"]:
            await self.delete_message(msg)
            await self.notify(group.main_group, circle.topic_id, reject_reason(intent["reason"], self.cfg.rules))
            if intent["record_early"] and intent["identity"]:
                await self.handle_early_drop(group, intent["identity"], msg)
            return
        drop_id = commit_drop(self.conn, circle, intent, handle, tweet_id, now)
        try:
            # The member's own message is removed only once the replacement is on the
            # wall. Clearing it first meant a failed send took both their link and the
            # number, leaving nothing to show for the try.
            entry_msg = await self.send_entry(group.main_group, circle.topic_id,
                                             intent["entry"])
        except Exception:
            log.exception("entry send failed, rolling back the drop")
            rollback_drop(self.conn, drop_id)
            # Their own message is still in the circle, so they can see the drop did
            # not go through. A permanent error line on top of that would only clutter
            # the circle, so it is posted as a notice that clears itself.
            await self.notify(group.main_group, circle.topic_id, text.DROP_SEND_FAILED)
            return
        await self.delete_message(msg)
        mark_drop_sent(self.conn, drop_id["drop_id"], entry_msg.id)
        await self.post_divider(group, circle)

    async def reply_cmd(self, group, msg, body, buttons=None, parse_mode=None):
        """Answer a command, and clean up only what was in the wrong place.

        A command typed inside a circle is junk: the circle is for entries, and
        admin chatter there is what made the topic unreadable. That one is removed and
        the answer goes to the prison topic.

        A command typed in the prison topic, or in the main chat, is already where it
        belongs and is left alone. Deleting every command took away the admin's own
        record of what they ran, which is not what was asked for.
        """
        topic = topic_id_of(msg)
        prison = group.topics["prison"]
        in_circle = topic is not None and group.circle_by_topic(topic) is not None

        if in_circle:
            await self.delete_message(msg)
            return await self.send_to_topic(
                group.main_group, prison, body,
                buttons=buttons, parse_mode=parse_mode, link_preview=False,
            )
        return await msg.reply(body, buttons=buttons, parse_mode=parse_mode)

    async def post_divider(self, group, circle):
        """Keep exactly one 'read rules' line, sitting at the very end of the circle.

        Posting a fresh divider after every drop made the topic fill with duplicates,
        so the previous one is deleted and the new one takes its place at the bottom.
        """
        key = f"divider_msg:{group.key}:{circle.key}"
        previous = dbm.q1(self.conn, "SELECT value FROM kv WHERE key=?", (key,))
        if previous and previous["value"]:
            try:
                await self.client.delete_messages(group.main_group, int(previous["value"]))
            except Exception as exc:
                log.debug("old divider in %s not deletable: %s", circle.key, exc)
        try:
            sent = await self.send_to_topic(
                group.main_group,
                circle.topic_id,
                text.divider_html(cards.rules_link(group), circle.kind),
                parse_mode="html",
            )
        except Exception as exc:
            log.warning("divider failed in %s: %s", circle.key, exc)
            return
        dbm.ex(self.conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
               (key, str(sent.id)))

    async def handle_early_drop(self, group, identity, msg):
        now = utcnow()
        attempts, warned = idm.record_early_drop(self.conn, identity.id, group.key, now)
        if not warned:
            return
        pun = dbm.q1(
            self.conn,
            "SELECT * FROM punishments WHERE identity_id=? AND reason='early_drop' ORDER BY id DESC LIMIT 1",
            (identity.id,),
        )
        if pun is None:
            return
        outbox.queue_punish(
            self.conn,
            identity.id,
            None,
            {"kind": "warn", "level": pun["level"], "duration": "4h", "reason": "early_drop", "mode": "bot_auto"},
        )
        mention = f"@{identity.tg_username}" if identity.tg_username else f"identity {identity.id}"
        public = text.PRISON_WARN_PUBLIC.format(
            mention=mention,
            level=pun["level"],
            max=self.cfg.rules.max_warnings,
            duration="4h",
            expires=pun["expires_at"][:16],
        )
        outbox.enqueue(
            self.conn,
            group.key,
            "public_warning",
            {"chat_id": group.main_group, "topic_id": group.topics["prison"], "text": public, "punishment_id": pun["id"]},
        )

    async def delete_message(self, msg):
        try:
            await self.client.delete_messages(msg.chat_id, msg.id)
            log.info("deleted member msg chat=%s id=%s", msg.chat_id, msg.id)
        except errors.FloodWaitError as exc:
            log.warning("delete floodwait %ss, waiting", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            await self.client.delete_messages(msg.chat_id, msg.id)
        except Exception as exc:
            log.warning("delete FAILED chat=%s id=%s err=%s: %s", msg.chat_id, msg.id, type(exc).__name__, exc)

    async def on_message(self, event):
        msg = event.message
        if not msg or not msg.text:
            return
        sender = getattr(msg, "sender", None)
        if sender is not None and getattr(sender, "bot", False):
            for gkey, group in self.groups.items():
                if msg.chat_id in (group.main_group, group.mod_group):
                    uname = (getattr(sender, "username", "") or "").lower()
                    if uname and uname == group.mod_bot_username.lower():
                        missrose.ingest(self.conn, msg.text, source_bot=uname)
            return
        group = None
        for g in self.cfg.groups:
            if msg.chat_id == g.main_group:
                group = g
                break
        if group is None:
            return
        if msg.text.startswith("/"):
            await self.on_command(event, group, msg)
            return
        circle = find_target(self.cfg, self.conn, group, msg)
        if circle is not None:
            await self.handle_entry_message(event, group, circle, msg)

    async def on_command(self, event, group, msg):
        if msg.text.startswith("/xhandle") or msg.text.startswith("/bind"):
            await self.on_bind_command(group, msg)
            return
        if msg.text.startswith("/appeal"):
            await self.cmd_appeal(group, msg)
            return
        # "check" is the correct spelling; "/chek" stays as an alias so nobody who
        # already learned the old one gets silently ignored.
        if msg.text.startswith(("/check", "/chek")):
            await self.cmd_check(group, msg)
            return
        if not is_admin(group, msg.sender_id):
            await self.reply_cmd(group, msg, text.ADMIN_ONLY)
            return
        parts = msg.text.split()
        cmd = parts[0].split("@")[0].lower()
        if cmd == "/del":
            await self.cmd_del(group, msg, parts)
        elif cmd == "/label":
            await self.cmd_label(group, msg, parts)
        elif cmd in ("/addvip", "/delvip"):
            await self.cmd_vip(group, msg, parts, cmd)
        elif cmd == "/cleared":
            await self.cmd_cleared(group, msg, parts)
        elif cmd == "/shift":
            await self.cmd_shift(group, msg, parts)
        elif cmd == "/health":
            await self.cmd_health(group, msg)
        elif cmd in ("/approve_alt", "/reject_alt"):
            await self.cmd_alt(group, msg, cmd, parts)
        # Anything else is not ours. MissRose answers /mute, /unmute, /warn and friends
        # directly, and replying "Unknown command" on top of her reply just added a
        # second, contradictory message to the same command.

    async def cmd_check(self, group, msg):
        """/check <telegram_username|x_username> - sample two links and judge them.

        Admin only. Letting a member check themselves tells them when they are being
        looked at, and an honest member who knows that can simply engage only around the
        times they expect to be sampled. Only admins get to run a check.

        Reading a thread means opening it in a browser, so this takes a minute or two.
        """
        parts = msg.text.split()
        if len(parts) < 2:
            await self.reply_cmd(group, msg, text.USAGE_CHECK)
            return

        username = parts[1]
        matches = lookup.find_identities(self.conn, username)
        if not matches:
            await self.reply_cmd(group, msg, text.CHECK_NOT_FOUND.format(name=username))
            return
        if len(matches) > 1:
            await self.reply_cmd(group, msg, text.CHECK_AMBIGUOUS.format(name=username))
            return
        identity_id = matches[0]

        caller_admin = is_admin(group, msg.sender_id)
        if not caller_admin:
            await self.reply_cmd(group, msg, text.ADMIN_ONLY)
            return

        try:
            self.limits.check(msg.sender_id, is_admin=caller_admin)
        except check_limits.Throttle as exc:
            await self.reply_cmd(group, msg, text.CHECK_THROTTLED.format(reason=str(exc)))
            return

        drop = lookup.latest_drop(self.conn, identity_id)
        if not drop:
            await self.reply_cmd(group, msg, text.CHECK_NO_DROP)
            return

        circle = group.circle_by_key(drop["circle_key"])
        if circle is None:
            await self.reply_cmd(group, msg, text.CHECK_NO_DROP.format(name=username))
            return

        await self.reply_cmd(group, msg, text.CHECK_STARTED.format(name=username))
        try:
            result = await self.limits.run(
                msg.sender_id, self.run_check, identity_id, circle
            )
        except check_limits.Throttle as exc:
            await self.reply_cmd(group, msg, text.CHECK_THROTTLED.format(reason=str(exc)))
            return
        except Exception as exc:
            log.exception("check failed for %s", username)
            await self.reply_cmd(group, msg, text.CHECK_FAILED.format(error=type(exc).__name__))
            return

        body, buttons = sampling.build_card(result)

        if result.get("verdict") == "violated":
            case_id = None
            try:
                case_id = sampling.open_case_for(self.conn, result, self.cfg.rules,
                                                 result.get("identity", {}).get("id"))
            except Exception:
                log.exception("could not open case for verified defaulter")
            if case_id:
                body = body + f"\n\nCase #{case_id} opened for admin review."
                buttons = buttons or [[{"text": "Review case",
                                        "url": cards.rules_link(group)}]]

        # A member only learns whether they passed. The sample size, the window, the
        # per-link detail and the jump buttons stay with admins: publishing any of it
        # tells members how the sampling works, and they would simply engage only when
        # they expect to be picked.
        word = text.CHECK_VERDICT_WORDS.get(result.get("verdict"), "REVIEW")
        # build_card returns plain data; Telegram rejects dicts, so it has to become
        # real inline buttons or the whole result is lost after the work is done.
        await self.reply_cmd(group, msg, text.CHECK_RESULT_ADMIN.format(
            name=username, verdict=word, body=body),
            buttons=self.build_buttons(buttons), parse_mode="html")

    async def run_check(self, identity_id, circle):
        """Run the sampled check with the browser reader."""
        from engine.x_reader import browser
        from engine.x_reader import comment_reader

        xr = self.cfg.x_reader
        cookie_path = xr.cookie_file or os.path.join(self.cfg.secrets.cookie_dir, "header_acct02.txt")
        header = browser.load_cookie(cookie_path)

        def make_reader():
            return comment_reader.make_reader(
                header,
                max_scrolls=xr.browser_max_scrolls,
                idle_rounds=xr.browser_idle_rounds,
                pause=xr.browser_pause,
            )

        reader = make_reader()
        return await sampling.evaluate_sampled_async(
            self.conn, identity_id, circle, xr.sample_size,
            random.SystemRandom(), reader,
            need_comment=circle.kind in ("engagement", "repost"),
            confirm=xr.confirm_sample,
        )

    async def on_bind_command(self, group, msg):
        """/xhandle binds the first handle from the welcome flow (member allowed).
        /bind <tg_user_id> <x_username> changes a handle (admin only)."""
        if msg.text.startswith("/bind"):
            await self.cmd_bind(group, msg)
            return
        parts = msg.text.split()
        if len(parts) < 2:
            await self.reply_cmd(group, msg, text.USAGE_XHANDLE)
            return
        handle = parts[1].lstrip("@")
        existing = idm.resolve_identity(self.conn, msg.sender_id)
        if existing is not None:
            await self.reply_cmd(group, msg, text.BIND_MEMBER_BLOCKED)
            return
        try:
            identity, _ = idm.bind_handle(self.conn, msg.sender_id, getattr(msg.sender, "username", None), handle)
        except ValueError as exc:
            holder = idm.identity_by_handle(self.conn, handle)
            if holder is not None:
                alt_id = idm.open_alt_case(
                    self.conn,
                    holder.id,
                    primary_account_id=_primary_account_id(self.conn, holder.id),
                    alt_tg_user_id=msg.sender_id,
                    signals=["hard:same_x_handle"],
                    score=100,
                )
                quarantine_account(self.conn, msg.sender_id)
                outbox.enqueue(
                    self.conn,
                    group.key,
                    "alert",
                    {
                        "chat_id": group.mod_group,
                        "text": text.ALT_ALERT.format(
                            case=alt_id,
                            handle=handle,
                            primary=holder.x_handle,
                            new_user=msg.sender_id,
                        ),
                    },
                )
            await self.reply_cmd(group, msg, text.QUARANTINE_NOTE)
            return
        await self.reply_cmd(group, msg, text.XHANDLE_BOUND.format(handle=identity.x_handle))

    async def cmd_bind(self, group, msg):
        """Admin only handle change."""
        parts = msg.text.split()
        if not is_admin(group, msg.sender_id):
            await self.reply_cmd(group, msg, text.BIND_MEMBER_BLOCKED)
            return
        if len(parts) < 3:
            await self.reply_cmd(group, msg, text.USAGE_BIND)
            return
        target = parts[1].lstrip("@")
        handle = parts[2].lstrip("@")
        if not target.lstrip("-").isdigit():
            await self.reply_cmd(group, msg, text.USAGE_BIND)
            return
        previous = idm.resolve_identity(self.conn, int(target))
        old_handle = previous.x_handle if previous else None
        try:
            identity = idm.rebind_handle(self.conn, int(target), handle)
        except LookupError:
            await self.reply_cmd(group, msg, text.BIND_NO_IDENTITY.format(tg=target))
            return
        except ValueError as exc:
            await self.reply_cmd(group, msg, text.BIND_IN_USE.format(new=handle) if "already bound" in str(exc) else str(exc))
            return
        await self.reply_cmd(group, msg, 
            text.BIND_DONE.format(
                tg=identity.tg_username or target,
                old=old_handle or identity.x_handle,
                new=identity.x_handle,
                at=dbm.now_iso()[11:16],
            )
        )

    async def cmd_appeal(self, group, msg):
        identity = idm.resolve_identity(self.conn, msg.sender_id)
        if identity is None:
            await self.reply_cmd(group, msg, text.APPEAL_NO_CASE)
            return
        case = dbm.q1(
            self.conn,
            "SELECT id FROM cases WHERE identity_id=? AND status IN (?,?) ORDER BY id DESC LIMIT 1",
            (identity.id, dbm.CASE_OPEN, dbm.CASE_ACTIONED),
        )
        if case is None:
            await self.reply_cmd(group, msg, text.APPEAL_NO_CASE)
            return
        reason = msg.text.split(None, 1)[1].strip() if len(msg.text.split(None, 1)) > 1 else "no reason given"
        appeal_key = f"appeal:{identity.id}:{case['id']}"
        if dbm.q1(self.conn, "SELECT value FROM kv WHERE key=?", (appeal_key,)):
            await self.reply_cmd(group, msg, text.APPEAL_RATE_LIMIT)
            return
        dbm.ex(self.conn, "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (appeal_key, dbm.now_iso()))
        dbm.ex(
            self.conn,
            "INSERT INTO punishments (identity_id, case_id, kind, level, expires_at, reason, actor_tg_user_id, mode, issued_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (identity.id, case["id"], "appeal", None, None, reason[:200], msg.sender_id, "member_appeal", dbm.now_iso()),
        )
        outbox.enqueue(
            self.conn,
            group.key,
            "alert",
            {
                "chat_id": group.mod_group,
                "text": f"Appeal on case #{case['id']} from @{identity.tg_username or identity.id}: {reason}",
            },
        )
        await self.reply_cmd(group, msg, text.APPEAL_SAVED.format(case=case["id"], at=dbm.now_iso()[11:16]))

    async def cmd_del(self, group, msg, parts):
        if len(parts) < 2:
            await self.reply_cmd(group, msg, text.USAGE_DEL)
            return
        circle_key, position = None, None
        try:
            if parts[1].isdigit():
                tid = topic_id_of(msg)
                circle = group.circle_by_topic(tid) if tid else None
                if circle is None:
                    await self.reply_cmd(group, msg, text.USAGE_DEL)
                    return
                circle_key, position = circle.key, int(parts[1])
            else:
                circle_key, position = parts[1], int(parts[2])
        except (IndexError, ValueError):
            await self.reply_cmd(group, msg, text.USAGE_DEL)
            return
        reason = " ".join(parts[3:]) if len(parts) > 3 else None
        result = delete_wrong_link(self.conn, circle_key, position, msg.sender_id, reason)
        if result is None:
            await self.reply_cmd(group, msg, text.NOT_FOUND.format(pos=position, circle=circle_key))
            return
        if result["blocked"] == "open_case":
            await self.reply_cmd(group, msg, f"#{position} has an open case, resolve the case first.")
            return
        drop = result["drop"]
        try:
            await self.client.delete_messages(group.main_group, [drop["msg_id"]])
        except Exception as exc:
            log.warning("entry delete failed: %s", exc)
            await self.reply_cmd(group, msg, text.DEL_FAIL.format(pos=position))
            return
        await self.reply_cmd(group, msg, 
            text.DELETED_NOTE.format(
                pos=position,
                circle=circle_key,
                reason=drop["deleted_reason"],
                at=dbm.now_iso()[11:16],
            )
        )

    async def cmd_label(self, group, msg, parts):
        """Label an entry, from inside its circle or by naming the circle.

        Only working inside the circle topic made it unusable from the prison topic,
        while /del accepted an explicit circle key. Both now take the same two forms.
        """
        tid = topic_id_of(msg)
        circle = group.circle_by_topic(tid) if tid else None
        if len(parts) < 3:
            await self.reply_cmd(group, msg, text.USAGE_LABEL)
            return
        if parts[1].isdigit():
            if circle is None:
                await self.reply_cmd(group, msg, text.USAGE_LABEL)
                return
            circle_key, position, label = circle.key, int(parts[1]), parts[2]
        else:
            try:
                circle_key, position, label = parts[1], int(parts[2]), parts[3]
            except (IndexError, ValueError):
                await self.reply_cmd(group, msg, text.USAGE_LABEL)
                return
        drop = label_drop(self.conn, circle_key, position, label)
        if drop is None:
            await self.reply_cmd(group, msg, text.LABEL_BAD.format(status="(none unlabeled)"))
            return
        await self.reply_cmd(group, msg, 
            text.LABEL_DONE.format(
                pos=position,
                label=label,
                admin=getattr(msg.sender, "username", msg.sender_id),
                at=dbm.now_iso()[11:16],
            )
        )

    async def cmd_vip(self, group, msg, parts, cmd):
        if len(parts) < 2:
            await self.reply_cmd(group, msg, text.USAGE_VIP)
            return
        identity = idm.set_vip_by_username(self.conn, parts[1], cmd == "/addvip")
        if identity is None:
            await self.reply_cmd(group, msg, text.VIP_MISSING.format(tg=parts[1]))
            return
        await self.reply_cmd(group, msg, 
            text.VIP_SET.format(
                tg="@" + parts[1].lstrip("@"),
                state="granted" if cmd == "/addvip" else "revoked",
                at=dbm.now_iso()[11:16],
            )
        )

    async def cmd_shift(self, group, msg, parts):
        action = parts[1] if len(parts) > 1 else "list"
        if action == "list":
            await self.reply_cmd(group, msg, shift.describe(self.conn))
            return
        if action == "set" and len(parts) >= 6:
            try:
                shift.upsert_shift(
                    self.conn,
                    int(parts[2].lstrip("@")),
                    parts[3],
                    parts[4],
                    parts[5],
                    parts[6] if len(parts) > 6 else "1111111",
                )
            except ValueError as exc:
                await self.reply_cmd(group, msg, f"shift error: {exc}")
                return
            await self.reply_cmd(group, msg, shift.describe(self.conn))
            return
        if action == "del" and len(parts) >= 3:
            n = shift.delete_shift(self.conn, int(parts[2].lstrip("@")))
            await self.reply_cmd(group, msg, f"removed {n} shift row(s) for {parts[2]}.")
            return
        await self.reply_cmd(group, msg, "Usage: /shift list | /shift set <tg_id> <TZ> <start> <end> [days 1111111] | /shift del <tg_id>")

    async def cmd_cleared(self, group, msg, parts):
        """Clear a member by username or id, or by replying to one of their messages.

        Asking an admin to look up a numeric id they have no way of knowing is a
        pointless extra step, so a Telegram username works just as well.
        """
        target = None
        if len(parts) >= 2:
            token = parts[1].lstrip("@")
            if token.isdigit():
                target = idm.resolve_identity(self.conn, int(token))
            else:
                found = lookup.find_identities(self.conn, token)
                if len(found) == 1:
                    target = idm.identity_by_id(self.conn, found[0])
                elif len(found) > 1:
                    await self.reply_cmd(group, msg, text.CHECK_AMBIGUOUS.format(name=token))
                    return
                else:
                    by_handle = dbm.q1(
                        self.conn, "SELECT id FROM identities WHERE LOWER(x_handle)=?", (token.lower(),))
                    if by_handle:
                        target = idm.identity_by_id(self.conn, by_handle["id"])
        if target is None and msg.reply_to_msg_id:
            target = _identity_for_entry(self.conn, msg.reply_to_msg_id)

        if target is None:
            await self.reply_cmd(group, msg, text.USAGE_CLEARED)
            return

        reset = idm.reset_member(self.conn, target.id, closed_by=msg.sender_id)
        await self.reply_cmd(group, msg, 
            text.CLEARED.format(n=reset["obligations"], tg=target.tg_username or target.id,
                                at=dbm.now_iso()[11:16])
            + text.CLEARED_DETAIL.format(cases=reset["cases"],
                                         intents=reset["punish_intents_cancelled"])
        )

    async def cmd_health(self, group, msg):
        ages = []
        # Only the services that actually exist. x_reader is gone: its GraphQL path
        # could never authenticate, and listing it here reported a permanent DOWN that
        # looked like a fault rather than a service that was removed on purpose.
        for svc in ("queue_bot", "userbot", "scheduler"):
            row = dbm.q1(self.conn, "SELECT value FROM kv WHERE key=?", (f"hb:{svc}",))
            if row is None:
                ages.append(f"{svc}=DOWN")
            else:
                try:
                    then = datetime.fromisoformat(row["value"])
                    delta = int((datetime.now(timezone.utc) - then).total_seconds())
                    ages.append(f"{svc}={delta}s")
                except ValueError:
                    ages.append(f"{svc}=??")
        n_open = dbm.q1(self.conn, "SELECT COUNT(*) AS n FROM cases WHERE status='open'")["n"]
        await self.reply_cmd(group, msg, " | ".join(ages) + f" | open_cases={n_open}")

    async def cmd_alt(self, group, msg, cmd, parts):
        if len(parts) < 2 or not parts[1].isdigit():
            await self.reply_cmd(group, msg, "Usage: /approve_alt <id> or /reject_alt <id>")
            return
        alt_id = int(parts[1])
        row = dbm.q1(self.conn, "SELECT * FROM alt_cases WHERE id=?", (alt_id,))
        if row is None or row["status"] != "open":
            await self.reply_cmd(group, msg, f"alt case {alt_id} not open")
            return
        decided = cmd == "/approve_alt"
        dbm.ex(
            self.conn,
            "UPDATE alt_cases SET status=?, decided_by=?, decided_at=? WHERE id=?",
            ("approved" if decided else "rejected", msg.sender_id, dbm.now_iso(), alt_id),
        )
        if decided:
            idm.blacklist_identity(self.conn, row["identity_id"])
            idm.propagate_to_identity(self.conn, row["identity_id"], "banned")
            kind = "blacklist"
        else:
            if row["alt_account_id"]:
                dbm.ex(self.conn, "UPDATE accounts SET status='active' WHERE id=?", (row["alt_account_id"],))
            kind = "unmute"
        dbm.ex(
            self.conn,
            "INSERT INTO punishments (identity_id, case_id, kind, level, expires_at, reason, actor_tg_user_id, mode, issued_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (row["identity_id"], None, kind, None, None, f"alt_case:{alt_id}", msg.sender_id, "admin_manual", dbm.now_iso()),
        )
        await self.reply_cmd(group, msg, 
            f"alt case {alt_id} {'approved, identity banned and handle blacklisted' if decided else 'rejected, account released'}."
        )

    async def on_reaction(self, event):
        # events.Raw hands over the update object itself, so read it straight off the
        # event. Reaching for event.update raised AttributeError on every single
        # reaction, which silently meant no engagement was ever recorded.
        u = getattr(event, "update", event)

        try:
            msg_id = u.msg_id
        except AttributeError:
            log.warning("reaction event without msg_id: %s", u)
            return
        user_id = getattr(u.actor, "user_id", 0) or 0
        # new_reactions holds ReactionEmoji / ReactionCustomEmoji directly, which carry
        # .emoticon and .document_id. Reading .emoji here always came back empty, so
        # every heart was treated as "not a heart" and no engagement was ever credited.
        emoji = ""
        if u.new_reactions:
            emoji = _reaction_emoji(u.new_reactions[0])
        if not msg_id or not user_id:
            log.warning("reaction event empty msg_id=%s user_id=%s", msg_id, user_id)
            return
        drop_id = store_reaction(self.conn, msg_id, user_id, emoji, utcnow())
        log.info("reaction msg=%s user=%s emoji=%r -> drop=%s", msg_id, user_id, emoji, drop_id)

    async def on_callback(self, event):
        data = event.data.decode()
        group = None
        for g in self.cfg.groups:
            if event.chat_id in (g.main_group, g.mod_group):
                group = g
                break
        if group is None:
            await event.answer("not registered", cache_time=1)
            return
        result = cards.apply_callback(self.conn, data, event.sender_id, group.admin_user_ids)
        if not result["ok"]:
            await event.answer(result["reason"], cache_time=1)
            return
        case_id = int(data.split(":")[1])
        if result["action"] == "warn":
            case_row = dbm.q1(self.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
            identity = idm.identity_by_id(self.conn, case_row["identity_id"])
            ecase = eng.Case(
                id=case_row["id"],
                identity_id=case_row["identity_id"],
                drop_id=case_row["drop_id"],
                missing_count=case_row["missing_count"],
                status=case_row["status"],
            )
            action = pm.next_action(identity, ecase, self.cfg.rules)
            if action is not None:
                pm.dispatch(
                    self.conn,
                    ecase,
                    action,
                    "admin_manual",
                    actor_tg_user_id=event.sender_id,
                    dry_run=False,
                    execute=lambda intent: outbox.queue_punish(self.conn, intent["identity_id"], case_id, intent),
                )
        loc = dbm.q1(self.conn, "SELECT value FROM kv WHERE key=?", (f"card:{case_id}",))
        if loc:
            try:
                chat_id, msg_id = json.loads(loc["value"])
                await self.client.edit_message(
                    chat_id,
                    msg_id,
                    buttons=None,
                )
            except Exception as exc:
                log.warning("card edit failed case=%s err=%s", case_id, exc)
        await event.answer(result["action"], cache_time=1)

    async def drain_outbox(self):
        while True:
            try:
                rows = outbox.pending(self.conn, limit=10)
                for row in rows:
                    payload = outbox.payload_of(row)
                    try:
                        await self.send_outbox_item(row["kind"], payload)
                        outbox.mark_sent(self.conn, row["id"])
                    except errors.FloodWaitError as exc:
                        outbox.bump_attempt(self.conn, row["id"], f"floodwait:{exc.seconds}")
                        await asyncio.sleep(exc.seconds + 1)
                    except Exception as exc:
                        outbox.bump_attempt(self.conn, row["id"], f"{type(exc).__name__}:{exc}")
                        log.error("outbox send failed id=%s err=%s", row["id"], exc)
            except Exception as exc:
                log.error("outbox drain error: %s", exc)
            await asyncio.sleep(2)

    async def send_outbox_item(self, kind, payload):
        if kind == "divider":
            await self.send_to_topic(
                payload["chat_id"], payload["topic_id"], payload["text"], parse_mode="html",
            )
            return
        if kind == "defaulter_card":
            msg = await self.send_to_topic(payload["chat_id"], payload.get("topic_id"), payload["text"])
            dbm.ex(
                self.conn,
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                (f"card:{payload['case_id']}", json.dumps([msg.chat_id, msg.id])),
            )
            return
        if kind in ("public_warning", "alert"):
            await self.send_to_topic(payload["chat_id"], payload.get("topic_id"), payload["text"])
            return
        log.warning("unknown outbox kind: %s", kind)

    async def heartbeat(self):
        while True:
            dbm.ex(
                self.conn,
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                ("hb:queue_bot", utcnow().isoformat(timespec="seconds")),
            )
            await asyncio.sleep(15)


def load_config_and_db():
    path = os.environ.get("HOTTI_CONFIG", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"))
    cfg = cfgmod.load(path)
    conn = dbm.connect(os.environ.get("HOTTI_DB", os.path.join(os.path.dirname(path), "data", "engine.sqlite")))
    return cfg, conn


def build_client(cfg):
    token = cfgmod.read_secret(cfg, "queue_bot_token")
    session_path = os.path.join(
        os.environ.get("HOTTI_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")),
        "queue_bot.session",
    )
    os.makedirs(os.path.dirname(session_path), exist_ok=True)

    async def _start():
        client = TelegramClient(session_path, cfg.app.api_id, cfg.app.api_hash)
        if not client.is_connected():
            await client.connect()
        # get_me() returns the cached bot user when the session file is already
        # authorized. Calling start(bot_token=...) again would hit
        # ImportBotAuthorizationRequest and earn a FloodWait on every restart.
        me = await client.get_me()
        if me is None:
            await client.start(bot_token=token)
        return client

    return None, _start


async def verify_admin(cfg, client):
    me = await client.get_me()
    log.info("queue bot getMe id=%s username=%s", me.id, me.username)
    for group in cfg.groups:
        entity = None
        try:
            entity = await client.get_entity(group.main_group)
        except Exception as exc:
            log.warning("direct resolve failed for %s: %s", group.main_group, exc)
            from services import peer_resolve

            if peer_resolve.prime_bot_session(client, group.main_group):
                try:
                    entity = await client.get_entity(group.main_group)
                except Exception as exc2:
                    raise SystemExit(
                        f"queue bot cannot resolve group {group.key} ({group.main_group}): {exc2}. "
                        f"Harvest peers first: python -m services.peer_resolve --harvest"
                    )
        if entity is None:
            raise SystemExit(f"queue bot cannot resolve group {group.key} ({group.main_group})")
        try:
            perms = await client.get_permissions(entity, me)
        except Exception as exc:
            raise SystemExit(f"queue bot cannot read permissions in {group.key}: {exc}")
        if not (perms.is_admin or perms.is_creator):
            raise SystemExit(f"queue bot is not admin in {group.key} ({group.main_group})")
        if not perms.delete_messages:
            raise SystemExit(f"queue bot lacks delete rights in {group.key} ({group.main_group})")
        log.info("verified admin+delete in %s (%s)", group.key, entity.id)


async def amain():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg, conn = load_config_and_db()
    _, start = build_client(cfg)
    client = await start()
    await verify_admin(cfg, client)
    bot = QueueBot(cfg, conn, client)

    @client.on(events.NewMessage)
    async def _(event):
        await bot.on_message(event)

    @client.on(events.Raw(UpdateBotMessageReaction))
    async def _(event):
        await bot.on_reaction(event)

    @client.on(events.CallbackQuery(pattern=b"case:"))
    async def _(event):
        await bot.on_callback(event)

    asyncio.create_task(bot.drain_outbox())
    from engine import checker as checker_mod
    downtime = checker_mod.record_startup_downtime(bot.conn)
    if downtime:
        log.warning("bot was down for %d seconds, checks over that window go to review",
                    downtime)
    asyncio.create_task(bot.heartbeat())
    log.info("queue bot ready, groups=%s", [g.key for g in cfg.groups])
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(amain())
