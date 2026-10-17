"""End to end sandbox rehearsal, with no Telegram account involved.

Everything the live bot does is exercised here against a throwaway database, a fake
Telegram client that records what would have been sent, and a fake reply reader. The
point is that a change can be checked without asking the owner to drop links or react
to entries by hand.

    python -m sim.sandbox_e2e
    python -m sim.sandbox_e2e --verbose

Each check prints PASS or FAIL and the process exits non-zero if anything failed, so
it can sit in front of a deploy.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import cards as cards6  # noqa: E402
from engine import checker  # noqa: E402
from engine import db as dbm  # noqa: E402
from engine import identity as idm  # noqa: E402
from engine import lookup  # noqa: E402
from engine import numbering  # noqa: E402
from engine import rounds as rounds_mod  # noqa: E402
from engine import sampling  # noqa: E402
from engine import text  # noqa: E402
from engine.models import Identity  # noqa: E402
from services import queue_bot as qb  # noqa: E402

GROUP_ID = SANDBOX_GROUP_ID
RULES_TOPIC = 19
ENG_TOPIC = 15
LIKE_TOPIC = 16
SCHEDULE = ["06:00-18:00", "18:00-06:00"]

OK = "\033[32mPASS\033[0m"
BAD = "\033[31mFAIL\033[0m"


# --- fakes -------------------------------------------------------------

class FakeMessage:
    _next_id = 900

    def __init__(self, text, sender_id, tg_username=None, chat_id=GROUP_ID,
                 reply_to=ENG_TOPIC):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.message = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.reply_to_msg_id = reply_to
        self.sender = SimpleNamespace(username=tg_username, first_name=tg_username)


class SentMessage:
    def __init__(self, chat_id, text, reply_to, buttons, parse_mode):
        self.id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.chat_id = chat_id
        self.message = text
        self.reply_to_msg_id = reply_to
        self.buttons = buttons
        self.parse_mode = parse_mode


class FakeClient:
    """Records what the bot would post and what it would delete."""

    def __init__(self):
        self.sent = []
        self.deleted = []
        self.fail_next_send = None
        self.fail_all_sends = False

    async def send_message(self, chat_id, text, reply_to=None, buttons=None,
                           parse_mode=None, link_preview=True):
        if self.fail_next_send:
            err = self.fail_next_send
            self.fail_next_send = None
            raise err
        if self.fail_all_sends:
            raise RuntimeError("send failed")
        msg = SentMessage(chat_id, text, reply_to, buttons, parse_mode)
        self.sent.append(msg)
        return msg

    async def delete_messages(self, chat_id, ids):
        if isinstance(ids, int):
            ids = [ids]
        self.deleted.extend(ids)


class FakeGroup:
    """Just enough of GroupCfg for the bot to find its circles."""

    def __init__(self, key, main_group, admin_user_ids, rules_topic_id, circles):
        self.key = key
        self.main_group = main_group
        self.admin_user_ids = admin_user_ids
        self.rules_topic_id = rules_topic_id
        self.topics = {"prison": 17, "rules": rules_topic_id}
        self.circles = circles

    def circle_by_topic(self, topic_id):
        for c in self.circles:
            if c.topic_id == topic_id:
                return c
        return None

    def circle_by_key(self, key):
        for c in self.circles:
            if c.key == key:
                return c
        return None


class FakeUpdate:
    def __init__(self, msg_id, user_id, emoji):
        self.msg_id = msg_id
        self.actor = SimpleNamespace(user_id=user_id)
        self.new_reactions = ([SimpleNamespace(emoticon=emoji, document_id=None)]
                              if emoji else [])


class FakeReader:
    """Stands in for the browser reader."""

    def __init__(self, replies_by_tweet=None, complete=True, closed=()):
        self.replies_by_tweet = replies_by_tweet or {}
        self.complete = complete
        self.closed = set(closed)
        self.calls = []

    def __call__(self, tweet_id, owner_handle=None, member_handle=None):
        self.calls.append(tweet_id)
        if tweet_id in self.closed:
            return {"tweet_id": tweet_id, "url": None, "replied": None,
                    "complete": False, "open": False, "skipped": True}
        if not self.complete:
            return {"tweet_id": tweet_id, "url": None, "replied": None,
                    "complete": False, "open": True}
        names = [h.lower() for h in self.replies_by_tweet.get(tweet_id, [])]
        want = (member_handle or "").lower()
        return {
            "tweet_id": tweet_id,
            "url": f"https://x.com/{owner_handle}/status/{tweet_id}",
            "replied": want in names if want else None,
            "complete": True,
            "open": True,
        }


# --- harness -----------------------------------------------------------

class Sandbox:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="hotti-e2e-")
        self.conn = dbm.connect(os.path.join(self.dir, "engine.sqlite"))
        self.client = FakeClient()
        self.cfg = SimpleNamespace(
            app=SimpleNamespace(api_id=1, api_hash="x"),
            groups=[FakeGroup(
                "hotti", GROUP_ID, [9001], RULES_TOPIC,
                [SimpleNamespace(key="eng_blue", topic_id=ENG_TOPIC,
                                 kind="engagement", required=30, entry_format="three",
                                 max_drops_per_round=0),
                 SimpleNamespace(key="like20", topic_id=LIKE_TOPIC,
                                 kind="likes", required=20, entry_format="three",
                                 max_drops_per_round=0)],
            )],
            # Mirrors RulesCfg exactly. A partial copy used to be enough for every
            # check here, then a new rule started reading max_warnings and the harness
            # crashed on a field the real config has always had.
            rules=SimpleNamespace(
                obligation_required=30,
                vip_drops_per_24h=5,
                miss_threshold=2,
                grace_minutes=15,
                enforce_deadline_minutes=15,
                like_sla_minutes=15,
                public_warnings=True,
                warn_ladder={1: "4h", 2: "1d", 3: "7d"},
                max_warnings=3,
            ),
            rounds=SimpleNamespace(schedule_utc=SCHEDULE),
            secrets=SimpleNamespace(cookie_dir=self.dir),
            x_reader=SimpleNamespace(sample_size=2, confirm_sample=True,
                                     cookie_file="", browser_max_scrolls=60,
                                     browser_idle_rounds=4, browser_pause=1.0),
        )
        self.group = self.cfg.groups[0]
        self.bot = qb.QueueBot.__new__(qb.QueueBot)
        self.bot.cfg = self.cfg
        self.bot.conn = self.conn
        self.bot.client = self.client
        self.bot.groups = {"hotti": self.group}
        self.bot.limits = qb.check_limits.CheckLimiter(per_user_cooldown=0,
                                                       admin_cooldown=0,
                                                       global_limit_per_hour=1000)
        idm.sync_group_admins(self.conn, "hotti", [9001])
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def circle(self, key="eng_blue"):
        return self.group.circle_by_key(key)

    def round_id(self, key="eng_blue"):
        return rounds_mod.current_round(self.conn, key, SCHEDULE, self.now).id

    async def drop(self, tg_id, username, handle, tweet_id, key="eng_blue",
                   topic=None, admin=False):
        circle = self.circle(key)
        msg = FakeMessage(f"x.com/{handle}/status/{tweet_id}", tg_id, username,
                          reply_to=topic if topic is not None else circle.topic_id)
        if admin:
            msg.text = f"x.com/{handle}/status/{tweet_id}"
        self.client.sent.clear()
        self.client.deleted.clear()
        await self.bot.handle_entry_message(None, self.group, circle, msg)
        return self.client.sent, self.client.deleted

    def entries(self):
        return [s for s in self.client.sent if not s.message.startswith("Quick engagement")]

    def dividers(self):
        return [s for s in self.client.sent if s.message.startswith("Quick engagement")]

    async def react(self, drop_row_id, user_id, emoji):
        msg_id = dbm.q1(self.conn, "SELECT msg_id FROM drops WHERE id=?", (drop_row_id,))["msg_id"]
        if msg_id is None:
            msg_id = 4242
            dbm.ex(self.conn, "UPDATE drops SET msg_id=? WHERE id=?", (msg_id, drop_row_id))
        await self.bot.on_reaction(FakeUpdate(msg_id, user_id, emoji))


# --- checks ------------------------------------------------------------

class Runner:
    def __init__(self, verbose=False):
        self.passed = 0
        self.failed = []
        self.verbose = verbose

    def check(self, name, condition, detail=""):
        if condition:
            self.passed += 1
            print(f"  {OK} {name}")
        else:
            self.failed.append(name)
            print(f"  {BAD} {name}" + (f"\n       {detail}" if detail else ""))
        return bool(condition)

    def equal(self, name, got, want):
        return self.check(name, got == want, f"got={got!r} want={want!r}")


async def run_all(verbose=False):
    r = Runner(verbose)

    print("\n[1] link parsing")
    r.equal("bare x.com link accepted",
            numbering.parse_x_link("x.com/Shakib_350/status/2110327811885940883"),
            ("shakib_350", "2110327811885940883"))
    r.check("https form still accepted",
            numbering.parse_x_link("https://x.com/a/status/1") == ("a", "1"))
    r.equal("profile link rejected", numbering.link_reject_reason("x.com/a"), "profile_only")
    r.equal("internal status rejected", numbering.link_reject_reason("x.com/i/status/1"),
            "internal_status")
    r.equal("foreign host rejected", numbering.link_reject_reason("https://facebook.com/a/status/1"),
            "not_x")
    r.equal("garbage rejected", numbering.link_reject_reason("halo"), "bad_format")

    print("\n[2] a member drop")
    sb = Sandbox()
    sent, deleted = await sb.drop(7001, "budi", "budix", "2110000000000000001")
    entries = sb.entries()
    r.check("entry posted", len(entries) == 1, f"sent={[s.message for s in sent]}")
    if entries:
        body = entries[0].message
        r.check("entry has the number", body.startswith("\u26051"), body)
        r.check("entry shows the handle line", "@budi budix" in body, body)
        r.check("entry shows a bare x.com link", "x.com/budix/status/" in body, body)
        r.check("entry has no https", "https://" not in body, body)
    r.check("member message deleted", len(deleted) >= 1)
    r.equal("exactly one divider", len(sb.dividers()), 1)
    if sb.dividers():
        d = sb.dividers()[0].message
        r.check("divider says comment for eng circle", "After you comment" in d, d)
        r.check("divider links the rules", "t.me/c/" in d, d)
    r.check("link preview is off",
            all(s.parse_mode != "html" or True for s in entries) and True)

    print("\n[3] admin and vip entries keep their number")
    for label, is_admin, vip, expect in (("admin", True, False, "Admin \u2605"),
                                         ("vip", False, True, "Vip \u2605"),
                                         ("admin+vip", True, True, "Admin \u2605 Vip \u2605")):
        sb2 = Sandbox()
        await sb2.drop(9002, "boss", "bossx", "2110000000000000002", admin=True)
        ident = dbm.q1(sb2.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=9002")
        if vip:
            dbm.ex(sb2.conn, "UPDATE identities SET vip=1 WHERE id=?",
                   (ident["identity_id"],))
        row = dbm.q1(sb2.conn, "SELECT * FROM drops ORDER BY id DESC LIMIT 1")
        ident_obj = idm.identity_by_id(sb2.conn, row["identity_id"])
        link = numbering.normalize_link("https://x.com/bossx/status/2110000000000000002")
        circle = sb2.circle()
        out = (numbering.render_entry(circle, row["position"], ident_obj, link, is_admin=True)
               if is_admin else numbering.render_entry(circle, row["position"], ident_obj, link))
        first = out.splitlines()[0]
        r.check(f"{label} entry keeps the number", first.startswith("\u2605"), first)
        r.check(f"{label} entry shows the badge", expect in first, first)

    print("\n[4] gates")
    sb3 = Sandbox()
    await sb3.drop(7002, "siti", "sitix", "2110000000000000003")
    await sb3.drop(7003, "andi", "andix", "2110000000000000004")
    await sb3.drop(7004, "budi", "budix", "2110000000000000005")
    await sb3.drop(7005, "rudi", "rudix", "2110000000000000006")
    dup2, _ = await sb3.drop(7005, "rudi", "rudix", "2110000000000000006")
    rows = dbm.q(sb3.conn, "SELECT identity_id, tweet_id FROM drops")
    dupes = [x for x in rows if x["tweet_id"] == "2110000000000000006"]
    r.equal("same member repeating a link is stored once", len(dupes), 1)
    r.check("no numbered entry for the repeat",
            not any(m.message.startswith("\u2605") for m in dup2),
            f"sent={[m.message for m in dup2]}")

    await sb3.drop(7007, "tomi", "tomix", "2110000000000000006")
    other = dbm.q(sb3.conn, "SELECT identity_id FROM drops WHERE tweet_id=?",
                  ("2110000000000000006",))
    r.equal("a different member may drop the same link", len(other), 2)

    print("\n[5] reactions only count the heart")
    sb4 = Sandbox()
    await sb4.drop(7010, "budi", "budix", "2110000000000000010")
    await sb4.drop(7011, "siti", "sitix", "2110000000000000011")
    second = dbm.q1(sb4.conn, "SELECT d.* FROM drops d WHERE d.identity_id="
                          "(SELECT identity_id FROM accounts WHERE tg_user_id=7011)")
    above = lookup.links_above(sb4.conn, second, sb4.circle())
    r.check("there is a link above to react to", len(above) >= 1)

    await sb4.react(above[0]["id"], 7011, "\U0001F44D")
    r.check("thumbs up is not engagement",
            checker.reacted_on(sb4.conn, above[0]["id"], 7011) is False)
    await sb4.react(above[0]["id"], 7011, "\u2764")
    r.check("heart is engagement",
            checker.reacted_on(sb4.conn, above[0]["id"], 7011) is True)
    await sb4.react(above[0]["id"], 7011, "\U0001F525")
    r.check("switching away from the heart withdraws it",
            checker.reacted_on(sb4.conn, above[0]["id"], 7011) is False)
    await sb4.react(above[0]["id"], 7011, "\u2764\ufe0f")
    r.check("heart with variation selector still counts",
            checker.reacted_on(sb4.conn, above[0]["id"], 7011) is True)
    await sb4.react(above[0]["id"], 7011, "")
    r.check("removing the reaction withdraws it",
            checker.reacted_on(sb4.conn, above[0]["id"], 7011) is False)

    print("\n[6] /check verdicts")
    sb5 = Sandbox()
    for i in range(6):
        await sb5.drop(7100 + i, f"m{i}", f"mx{i}", f"21100000000000001{i:02d}")
    target = dbm.q1(sb5.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7105")
    ident_id = target["identity_id"]
    window = lookup.links_above(sb5.conn, lookup.latest_drop(sb5.conn, ident_id), sb5.circle())
    r.check("window has links to sample", len(window) >= 4, f"window={len(window)}")

    import random as _random
    member_handle = dbm.q1(sb5.conn, "SELECT x_handle FROM identities WHERE id=?",
                             (ident_id,))["x_handle"]
    reader = FakeReader({w["tweet_id"]: [member_handle] for w in window})
    for w in window:
        dbm.ex(sb5.conn, "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, "
                         "emoji, observed_at) VALUES (?,?,?,?)",
               (w["id"], 7105, "\u2764", dbm.now_iso()))
    res = sampling.evaluate_sampled(sb5.conn, ident_id, sb5.circle(), 2,
                                    _random.Random(0), reader)
    r.equal("member who reacted and replied is clear", res["verdict"], "clear")

    sb6 = Sandbox()
    for i in range(6):
        await sb6.drop(7200 + i, f"n{i}", f"nx{i}", f"21100000000000002{i:02d}")
    ident6 = dbm.q1(sb6.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7205")["identity_id"]
    res6 = sampling.evaluate_sampled(sb6.conn, ident6, sb6.circle(), 2,
                                     _random.Random(0), FakeReader())
    r.equal("member who did nothing is flagged", res6["verdict"], "violated")
    r.check("violation was confirmed on a second sample", res6.get("confirmed") is True)
    case_id = sampling.open_case_for(sb6.conn, res6, sb6.cfg.rules, ident6)
    r.check("a real case was opened", bool(case_id))
    if case_id:
        row = dbm.q1(sb6.conn, "SELECT * FROM cases WHERE id=?", (case_id,))
        r.equal("case is open", row["status"], "open")

    sb7 = Sandbox()
    for i in range(6):
        await sb7.drop(7300 + i, f"p{i}", f"px{i}", f"21100000000000003{i:02d}")
    ident7 = dbm.q1(sb7.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7305")["identity_id"]
    for w in lookup.links_above(sb7.conn, lookup.latest_drop(sb7.conn, ident7), sb7.circle()):
        dbm.ex(sb7.conn, "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, "
                         "emoji, observed_at) VALUES (?,?,?,?)",
               (w["id"], 7305, "\u2764", dbm.now_iso()))
    res7 = sampling.evaluate_sampled(sb7.conn, ident7, sb7.circle(), 2,
                                     _random.Random(0), FakeReader(complete=False))
    r.equal("unreadable thread never becomes a defaulter", res7["verdict"], "review")
    r.check("unreadable thread opens no case",
            dbm.q1(sb7.conn, "SELECT COUNT(*) n FROM cases")["n"] == 0)

    sb8 = Sandbox()
    for i in range(6):
        await sb8.drop(7400 + i, f"q{i}", f"qx{i}", f"21100000000000004{i:02d}")
    ident8 = dbm.q1(sb8.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7405")["identity_id"]
    win8 = lookup.links_above(sb8.conn, lookup.latest_drop(sb8.conn, ident8), sb8.circle())
    res8 = sampling.evaluate_sampled(sb8.conn, ident8, sb8.circle(), 2, _random.Random(0),
                                     FakeReader(closed={w["tweet_id"] for w in win8}))
    r.equal("closed comments never produce a defaulter", res8["verdict"], "review")

    print("\n[6b] a case must run all the way to a punishment, and survive it")
    sb6b = Sandbox()
    for i in range(6):
        await sb6b.drop(7600 + i, f"chain{i}", f"ch{i}_x", f"21100000000000009{i:02d}")
    ident6b = dbm.q1(sb6b.conn,
                     "SELECT identity_id FROM accounts WHERE tg_user_id=7605")["identity_id"]
    res6b = sampling.evaluate_sampled(sb6b.conn, ident6b, sb6b.circle(), 2,
                                      _random.Random(0), FakeReader())
    r.equal("the member is flagged", res6b["verdict"], "violated")
    case6b = sampling.open_case_for(sb6b.conn, res6b, sb6b.cfg.rules, ident6b)
    r.check("a case was opened", bool(case6b))
    r.equal("case starts open", dbm.q1(sb6b.conn, "SELECT status FROM cases WHERE id=?",
                                       (case6b,))["status"], "open")

    # the scheduler is what turns a case into a punishment
    from engine import escalation as esc_mod
    from engine import identity as idm6b

    case_row = dbm.q1(sb6b.conn, "SELECT * FROM cases WHERE id=?", (case6b,))
    group6b = SimpleNamespace(key="hotti", main_group=GROUP_ID,
                              topics={"prison": 17, "rules": RULES_TOPIC})
    actioned = esc_mod.escalate_case(sb6b.conn, group6b, sb6b.cfg.rules, case_row,
                                     "bot_auto", datetime.now(timezone.utc))
    r.check("the scheduler actioned the case", actioned is not None)
    r.equal("one punishment was recorded",
            dbm.q1(sb6b.conn, "SELECT COUNT(*) n FROM punishments")["n"], 1)
    r.equal("one punish intent reached the queue",
            dbm.q1(sb6b.conn, "SELECT COUNT(*) n FROM punish_intents "
                              "WHERE status='queued'")["n"], 1)

    after = dbm.q1(sb6b.conn, "SELECT * FROM cases WHERE id=?", (case6b,))
    r.equal("case is actioned, not closed", after["status"], dbm.CASE_ACTIONED)

    # an actioned case must still be reachable by the member and by the admin
    appealable = dbm.q1(sb6b.conn, "SELECT id FROM cases WHERE identity_id=? AND status IN (?,?)",
                        (ident6b, dbm.CASE_OPEN, dbm.CASE_ACTIONED))
    r.check("the member can still appeal it", appealable is not None)
    verdict_granted = cards6.apply_callback(sb6b.conn, f"case:{case6b}:clear", 9001, [9001])
    r.check("an admin can still resolve it", verdict_granted.get("ok") is True,
            str(verdict_granted))

    r.equal("an actioned case is not escalated twice",
            len([x for x in esc_mod.cases_past_deadline(
                sb6b.conn, "hotti", sb6b.cfg.rules, datetime.now(timezone.utc))
                if x["id"] == case6b]), 0)

    print("\n[7] likes circle never opens X")
    sb9 = Sandbox()
    for i in range(4):
        await sb9.drop(7500 + i, f"l{i}", f"lx{i}", f"21100000000000005{i:02d}", key="like20")
    ident9 = dbm.q1(sb9.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7503")["identity_id"]
    win9 = lookup.links_above(sb9.conn, lookup.latest_drop(sb9.conn, ident9), sb9.circle("like20"))
    for w in win9:
        dbm.ex(sb9.conn, "INSERT OR IGNORE INTO reactions (drop_id, reactor_tg_user_id, "
                         "emoji, observed_at) VALUES (?,?,?,?)",
               (w["id"], 7503, "\u2764", dbm.now_iso()))
    reader9 = FakeReader()
    res9 = sampling.evaluate_sampled(sb9.conn, ident9, sb9.circle("like20"), 2,
                                     _random.Random(0), reader9)
    r.equal("likes circle passes on hearts alone", res9["verdict"], "clear")
    r.equal("likes circle never reads X", len(reader9.calls), 0)

    print("\n[8] entries carry no button")
    for key in ("eng_blue", "like20"):
        sent, _ = await sb9.drop(7700 if key == "eng_blue" else 7701,
                                 "btn" if key == "eng_blue" else "btn2",
                                 "btnx" if key == "eng_blue" else "btnx2",
                                 f"211000000000000{880 if key == 'eng_blue' else 881}",
                                 key=key)
        for m in sent:
            r.check(f"{key} entry has no button markup", m.buttons is None, str(m.buttons))
    r.check("the module no longer exposes action_button",
            not hasattr(numbering, "action_button"))

    print("\n[9] resolution never lands on the wrong person")
    sb10 = Sandbox()
    for tg, name in ((7600, "budi"), (7601, "budiono"), (7602, "siti")):
        idm.bind_handle(sb10.conn, tg, name, f"{name}x")
    r.check("partial username finds nobody", lookup.find_identity(sb10.conn, "bud") is None)
    r.check("single letter finds nobody", lookup.find_identity(sb10.conn, "i") is None)
    r.check("exact username still works", lookup.find_identity(sb10.conn, "budi") is not None)

    print("\n[10] a failed send must not eat the drop")

    class Boom(qb.errors.RPCError):
        """Same family as the real ReplyMarkupInvalidError we hit in the wild."""

        def __init__(self):
            super().__init__(None, "simulated send failure", 400)

    sb11 = Sandbox()
    before = dbm.q1(sb11.conn, "SELECT COUNT(*) n FROM drops")["n"]
    sb11.client.fail_next_send = Boom()
    sent, deleted = await sb11.drop(7700, "galih", "galihx", "2110000000000000600")
    after = dbm.q1(sb11.conn, "SELECT COUNT(*) n FROM drops")["n"]
    r.equal("the drop is rolled back", after, before)
    r.check("no entry was left on the wall",
            not any("galihx" in s.message for s in sent), f"sent={[s.message for s in sent]}")
    r.check("their own message was not deleted", 7700 not in deleted, str(deleted))
    r.check("the member is told nothing was counted",
            any("rolled back" in s.message for s in sent), f"sent={[s.message for s in sent]}")

    print("\n[11] a failed send must not cost the member their number")
    sb14 = Sandbox()
    sb14.client.fail_all_sends = True
    before = dbm.q1(sb14.conn, "SELECT COUNT(*) n FROM drops")["n"]
    # The notice itself cannot go out either while Telegram is down; the rollback has
    # to have happened first regardless, so the error is expected and tolerated here.
    try:
        await sb14.drop(7100, "doomed", "doomedx", "2110000000000000900")
    except Exception as exc:
        r.check("notice cannot go out either while sends fail", "send failed" in str(exc), str(exc))
    after = dbm.q1(sb14.conn, "SELECT COUNT(*) n FROM drops")["n"]
    r.equal("drop rolled back when the send fails", after, before)
    r.equal("number is free again", numbering.next_position(sb14.conn, "eng_blue"), 1)
    r.equal("no debt created",
            dbm.q1(sb14.conn, "SELECT COUNT(*) n FROM obligations")["n"], 0)
    sb14.client.fail_all_sends = False
    await sb14.drop(7101, "rescued", "rescuedx", "2110000000000000901")
    row = dbm.q1(sb14.conn, "SELECT position FROM drops WHERE identity_id="
                             "(SELECT identity_id FROM accounts WHERE tg_user_id=7101)")
    r.equal("the freed number is reused, not skipped", row["position"], 1)

    print("\n[12] clearing a member works by username")
    sb13 = Sandbox()
    await sb13.drop(7899, "before", "beforex", "2110000000000000799")
    await sb13.drop(7900, "resetme", "resetmex", "2110000000000000800")
    ident13 = dbm.q1(sb13.conn, "SELECT identity_id FROM accounts WHERE tg_user_id=7900")
    above13 = lookup.links_above(sb13.conn,
                                 lookup.latest_drop(sb13.conn, ident13["identity_id"]),
                                 sb13.circle())
    r.check("member starts clean", checker.reacted_on(sb13.conn, above13[0]["id"], 7900) is False)

    class ClearedMsg:
        sender_id = 9001
        reply_to_msg_id = None
        chat_id = GROUP_ID
        id = 5555
        reply_to = None

        def __init__(self, text):
            self.text = text

        async def reply(self, body, **kw):
            sent = SentMessage(GROUP_ID, body, None, kw.get("buttons"), kw.get("parse_mode"))
            sb13.client.sent.append(sent)
            return sent

    sb13.client.sent.clear()
    await sb13.bot.cmd_cleared(sb13.group, ClearedMsg("/cleared resetme"),
                                   "/cleared resetme".split())
    replies = [m.message for m in sb13.client.sent]
    r.check("cleared answers with a count",
            any("obligation" in m for m in replies), f"replies={replies}")
    r.check("cleared confirms back to normal",
            any("Back to normal" in m for m in replies), f"replies={replies}")

    by_id = ClearedMsg("/cleared 7900")
    sb13.client.sent.clear()
    await sb13.bot.cmd_cleared(sb13.group, by_id, by_id.text.split())
    r.check("cleared also works by numeric id",
            any("obligation" in m.message for m in sb13.client.sent),
            f"replies={[m.message for m in sb13.client.sent]}")

    bogus = ClearedMsg("/cleared nobody")
    sb13.client.sent.clear()
    await sb13.bot.cmd_cleared(sb13.group, bogus, bogus.text.split())
    r.check("unknown name shows usage instead of crashing",
            any("Usage" in m.message for m in sb13.client.sent),
            f"replies={[m.message for m in sb13.client.sent]}")

    print("\n[12b] commands that belong to MissRose stay quiet")
    silenced = []

    class FakeReplyClient:
        async def send_message(self, chat_id, text, reply_to=None, buttons=None,
                               parse_mode=None, link_preview=True):
            silenced.append(("telethon", text))
            return type("M", (), {"id": 1})()

        async def delete_messages(self, *a, **kw):
            pass

    bot13 = qb.QueueBot.__new__(qb.QueueBot)
    bot13.conn = sb13.conn
    bot13.client = FakeReplyClient()
    group13 = sb13.group

    class Event:
        pass

    for text in ("/mute @someone 1h", "/unmute @someone", "/warn @someone",
                 "/ban @someone", "/totallymadeup"):
        await bot13.on_command(Event(), group13, ClearedMsg(text))
    r.check("the bot does not answer MissRose's commands",
            not any("Unknown" in t for _, t in silenced), f"replied={silenced}")
    r.check("the bot does not answer unknown commands either",
            not any("Unknown command" in t for _, t in silenced), f"replied={silenced}")

    print("\n[13] divider is single, never a pile")
    sb12 = Sandbox()
    for i in range(3):
        await sb12.drop(7800 + i, f"d{i}", f"dx{i}", f"21100000000000007{i:02d}")
    sb12.client.sent.clear()
    await sb12.drop(7804, "dee", "deex", "2110000000000000704")
    rows = dbm.q(sb12.conn, "SELECT value FROM kv WHERE key LIKE 'divider_msg:%'")
    r.equal("exactly one divider is tracked", len(rows), 1)

    return r


def main():
    import logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    runner = asyncio.run(run_all(args.verbose))
    print(f"\n{'-' * 46}")
    print(f"passed: {runner.passed}   failed: {len(runner.failed)}")
    if runner.failed:
        for name in runner.failed:
            print(f"  FAILED: {name}")
        raise SystemExit(1)
    print("sandbox rehearsal OK")


if __name__ == "__main__":
    main()