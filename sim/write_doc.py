"""Emit docs/HOW_IT_WORKS.txt with column widths computed, never hand padded.

Hand padded tables came out ragged, which is exactly the mess that was sent to the
group. Widths are derived from the content, so every column lines up by construction.
"""

import pathlib

WIDTH = 78


def table(header, rows, gap=3, width=WIDTH):
    """Aligned table; the last column wraps so no line runs past width."""
    cols = len(header)
    lead_w = []
    for c in range(cols - 1):
        candidates = [len(header[c])] + [len(r[c]) if c < len(r) else 0 for r in rows]
        lead_w.append(max(candidates))

    indent = 2 + sum(lead_w) + gap * (cols - 1)
    last_w = max(20, len(header[cols - 1]), width - indent)

    def row_text(cells):
        parts = [cells[c].ljust(lead_w[c]) for c in range(cols - 1)]
        parts.append(cells[cols - 1])
        return ("  " + (" " * gap).join(parts)).rstrip()

    out = [row_text(list(header)),
           "  " + (" " * gap).join("-" * lead_w[c] for c in range(cols - 1))
           + " " * gap + "-" * last_w]

    for r in rows:
        lead = [(r[c] if c < len(r) else "") for c in range(cols - 1)]
        last = r[cols - 1] if cols - 1 < len(r) else ""
        lines = wrap(last, indent=0, width=last_w) if last else [""]
        out.append(row_text(lead + [lines[0]]))
        out.extend(" " * indent + extra for extra in lines[1:])
    return out


def wrap(text, indent=0, width=WIDTH):
    words = text.split()
    lines, cur = [], " " * indent
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur.rstrip())
            cur = " " * indent
        cur += w + " "
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def rule(title):
    return ["=" * WIDTH, title.center(WIDTH).rstrip(), "=" * WIDTH]


def build():
    L = []

    L += ["HOTTI ENGINE - HOW IT WORKS AND HOW TO USE IT"]
    L += wrap("For the group owner. Everything below was verified by running it, "
              "not inferred.")
    L += [""]

    # 1
    L += rule("1. WHAT THE BOT DOES")
    L += [""]
    L += wrap("Members drop an X link into a circle topic. The bot deletes their raw "
              "message, reposts it as a numbered entry, and puts the rules divider "
              "underneath. It then checks whether each member actually engaged with "
              "the links above them, raises cases on real defaulters, and drives "
              "MissRose for warnings.")
    L += [""]
    L += table(["CIRCLE", "TOPIC", "OWED", "WHAT A MEMBER OWES"], [
        ["eng_blue", "15", "30", "heart on the 30 entries above AND a comment on each of those X posts"],
        ["like20", "16", "20", "heart on the 20 entries above only. No comment. X is never opened"],
    ])
    L += [""]
    L += wrap("Only one emoji counts: HEART (U+2764). Anything else - thumbs up, fire, "
              "pink heart, sparkle heart, two hearts - is treated as no reaction at "
              "all. Removing the heart withdraws the credit immediately.")
    L += [""]

    # 2
    L += rule("2. THE ENTRY A MEMBER SEES")
    L += [""]
    L += ["  +47",
          "  @member_telegram_name their_x_handle",
          "  x.com/their_x_handle/status/2110293723489718312",
          ""]
    L += wrap("Admins and VIPs keep the badge AND the number:")
    L += ["", "  +48 Admin *", "  +49 Vip *", "  +50 Admin * Vip *", ""]
    L += wrap("The number is never dropped, because /del and /label both work by "
              "number. Positions are never renumbered - a deleted entry leaves a "
              "permanent gap, and that gap is the public defaulter trace.")
    L += [""]
    L += wrap("Underneath every entry sits one divider, exactly one, which moves to "
              "the bottom whenever a new entry lands:")
    L += ["",
          "  Quick engagement here (read rules)",
          "  After you comment, react (heart) to the entries above.",
          ""]
    L += wrap("On the likes circle the second line instead reads:")
    L += ["", "  After you like, react (heart) to the entries above.", ""]
    L += wrap("Links are rewritten to the bare \"x.com/...\" form with no scheme. The "
              "\"x.com/intent/like\" form was tested and rejected: it answers with a "
              "307 redirect and leaves Telegram's link preview blank, which members "
              "read as a dead link.")
    L += [""]

    # 3
    L += rule("3. MEMBER COMMANDS")
    L += [""]
    L += table(["ACTION", "EFFECT"], [
        ["drop a link in a circle topic",
         "binds the X handle on the first drop, then queues"],
        ["/xhandle <x_username>",
         "binds your handle once, before you have ever dropped"],
        ["/appeal <reason>",
         "files an appeal against a case that is still open"],
    ])
    L += [""]
    L += wrap("/xhandle works exactly once. Changing a handle afterwards is admin "
              "only, so members cannot swap to a fresh handle to dodge a case.")
    L += [""]

    # 4
    L += rule("4. ADMIN COMMANDS")
    L += [""]
    L += wrap("All of these are answered in the PRISON topic. A command typed inside "
              "a circle topic is deleted, because admin chatter in a circle is what "
              "made the topic unreadable.")
    L += [""]
    L += table(["COMMAND", "EFFECT"], [
        ["/check <username or id>", "checks two random links from that member's window"],
        ["/cleared <username or id>",
         "full reset: clears the debt, closes open cases, cancels queued punishments"],
        ["/bind <tg_id> <x_username>", "changes an existing handle, audited"],
        ["/del <circle_key> <position>",
         "deletes a wrong entry, grants the member a drop credit"],
        ["/label <circle_key> <pos> <wrong|defaulter>",
         "labels an entry deleted by hand"],
        ["/addvip <username> / /delvip <username>", "grants or revokes VIP"],
        ["/shift list | set | del", "admin coverage windows"],
        ["/health", "service heartbeats and open case count"],
        ["/approve_alt <id> / /reject_alt <id>", "decides an alt handle case"],
    ])
    L += [""]
    L += wrap("/check and /cleared accept either a Telegram username or an X handle, "
              "and matching is exact - a partial name finds nobody rather than "
              "guessing at a random person. Admins can check anyone; members cannot "
              "run /check at all, which is deliberate: telling a member when they are "
              "being looked at invites them to engage only around the times they "
              "expect to be sampled.")
    L += [""]
    L += wrap("/del grants a drop credit, so a member wrongly deleted does not have "
              "to repeat the whole obligation to drop again.")
    L += [""]

    # 5
    L += rule("5. HOW THE CHECKER DECIDES")
    L += [""]
    L += wrap("An admin runs /check <name>. The bot then:")
    L += ["",
          "  1. walks back to that member's most recent drop,",
          "  2. collects the links above it in the same circle,",
          "  3. drops any link whose comments are closed - nobody could have",
          "     commented on it, so counting it would invent a violation,",
          "  4. takes two links at random,",
          "  5. opens each post in a real browser and reads every reply,",
          "  6. for each link, requires BOTH signals: the heart on the entry and",
          "     the comment on X.",
          ""]
    L += wrap("Outcomes:")
    L += [""]
    L += table(["RESULT", "MEANING"], [
        ["PASS", "every sampled link, both signals present"],
        ["MISS", "every sampled link missing, confirmed on a second, different pair"],
        ["REVIEW", "anything unproven: a thread that could not be read to the end, "
                   "closed comments, or a mix"],
    ])
    L += [""]
    L += wrap("REVIEW is the important one. If a thread did not read to the end, the "
              "answer is not \"no comment\", it is \"we don't know\", and nobody is "
              "punished on it.")
    L += [""]
    L += ["Two extra guards:", ""]
    L += wrap("- Closed comments never count. Detected through X's syndication "
              "endpoint. If that probe fails for any reason the link is treated as "
              "closed, which is the safe direction.", indent=2)
    L += [""]
    L += wrap("- Downtime never counts against a member. Reactions only reach us "
              "while the bot is running, and Telegram does not let a bot read "
              "reactions back afterwards. If the bot was down across a member's "
              "window, the verdict is capped at REVIEW.", indent=2)
    L += [""]
    L += wrap("A MISS opens a case. The scheduler escalates any case still open past "
              "the enforce deadline, writing a punishment row and a queued intent. The "
              "case then becomes ACTIONED - not closed - so the member can still "
              "appeal it and an admin can still clear or confirm it from the card. An "
              "actioned case is never escalated twice.")
    L += [""]

    # 6
    L += rule("6. WHY LIKES CANNOT BE CHECKED")
    L += [""]
    L += wrap("This was tested directly rather than assumed.")
    L += [""]
    L += wrap("REPLIES CAN BE ENUMERATED. On the posts we checked, the reply list is "
              "real HTML: 45 links to /status/ on first view, 157 to 229 replies once "
              "scrolled to the end. All of it read in about 30 seconds per post.")
    L += [""]
    L += wrap("LIKES CANNOT. On the same posts the page shows only bare numbers - "
              "Views 57, 1, 52. There is no likes tab reachable, and of 145 links on "
              "the page, ZERO contain the word \"like\". There is no navigation to a "
              "list of who liked, so there is no way to answer the one question the "
              "checker needs: did this specific member like this specific post.")
    L += [""]
    L += wrap("The old code had a GraphQL \"Favoriters\" endpoint for this. That path "
              "is gone: our read-only account is suspended, and X rejects every "
              "authenticated API call with \"401 Could not authenticate you\" or "
              "\"Invalid or expired token\". There is no public API for \"who liked "
              "this tweet\" any more.")
    L += [""]
    L += wrap("So likes are simply not a signal we can verify, and the checker does "
              "not pretend otherwise.")
    L += [""]
    L += wrap("That is also why the requirement is a COMMENT, not a like: a comment "
              "is attributable, costs something, and cannot be faked from a second "
              "account the way a like can.")
    L += [""]
    L += wrap("IF LIKES MUST BE VERIFIED LATER, the only route is an X developer API "
              "account with paid access to the liking user list. That is a separate "
              "spend, not a code change.")
    L += [""]
    L += ["Re-check the finding any time, on any public post:", "",
          "  PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright \\",
          "    ./venv/bin/python -m services.x_probe_likes <handle> <tweet_id>", ""]

    # 7
    L += rule("7. WHERE IT STANDS TODAY")
    L += [""]
    L += ["  351 offline tests pass",
          "  77 sandbox rehearsal checks pass",
          "  7 static code guards pass",
          "  preflight passes",
          ""]
    L += wrap("The sandbox rehearsal runs the whole pipeline against a throwaway "
              "database, a fake Telegram, and a fake reader. It needs no Telegram "
              "account, so it can be run as often as wanted without asking anyone to "
              "drop links by hand. It is part of the test suite.")
    L += [""]
    L += wrap("NOT YET PROVEN: a command has never actually been delivered to "
              "MissRose. The enforcement mode is list_only, which deliberately blocks "
              "sending. The ladder fires only in list_then_auto or auto.")
    L += [""]
    L += table(["MODE", "BEHAVIOUR"], [
        ["list_only", "checks and records everything, sends NOTHING to MissRose. "
                      "Current setting."],
        ["list_then_auto", "fires the ladder after the enforce deadline, on a card "
                           "nobody touched."],
        ["auto", "fires immediately."],
    ])
    L += [""]
    L += wrap("list_only is the right launch value: it exercises the reader, the "
              "entries, the reactions and the case pipeline for a round or two, with "
              "no way to mute or ban anyone by mistake.")
    L += [""]

    # 8
    L += rule("8. TRADEOFFS YOU ARE ACCEPTING, STATED PLAINLY")
    L += [""]
    L += wrap("Sampling two links out of thirty is cheap and it is what keeps the "
              "checker inside the memory and time this box has. The cost is "
              "measurable, from 1000 simulated days:")
    L += [""]
    L += table(["OUTCOME", "MEASURED", "TARGET", "RESULT"], [
        ["Member wrongly flagged", "1.46%", "under 2%", "PASSES"],
        ["Real defaulter that escapes", "26.10%", "under 1%", "FAILS"],
    ])
    L += [""]
    L += wrap("The first number is the one that matters most: almost nobody gets "
              "falsely accused, because a violation needs two independent failing "
              "samples. The second is the price: roughly one in four genuine "
              "defaulters gets through, because out of 30 links only 4 are ever read.")
    L += [""]
    L += wrap("Raising the sample size to 3 would catch more, at the cost of reading "
              "six links per check, which roughly doubles the time per member and the "
              "load on a suspended cookie.")
    L += [""]

    # 9
    L += rule("9. RUNNING IT")
    L += [""]
    L += ["  ./venv/bin/python -m pytest tests/ -q",
          "      Offline. No Telegram, no network.",
          "",
          "  ./venv/bin/python -m sim.sandbox_e2e",
          "      Full pipeline rehearsal, no accounts needed.",
          "",
          "  ./venv/bin/python -m sim.simulate_rounds --seeds 1000",
          "",
          "  HOTTI_CONFIG=config.yaml ./venv/bin/python -m services.preflight",
          "      Exits non-zero when a required check fails, including the cookie",
          "      header being present and not world-readable.",
          ""]
    L += ["Read a post manually and see who replied:", "",
          "  PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright \\",
          "    ./venv/bin/python -m services.x_scrape <handle> <tweet_id> --show",
          ""]
    L += wrap("Back up with sqlite3 data/engine.sqlite \".backup 'backup.db'\", never "
              "a raw file copy of a live database.")
    L += [""]

    # 10
    L += rule("10. CURRENT SANDBOX CONFIGURATION")
    L += [""]
    L += table(["SETTING", "VALUE"], [
        ["Sandbox group", "Hottie test sandbox, SANDBOX_GROUP_ID"],
        ["Eng circle", "topic 15, 30 required"],
        ["Likes circle", "topic 16, 20 required"],
        ["Prison / warnings", "topic 17"],
        ["Rules", "topic 19"],
        ["Rounds", "fixed 06:00-18:00 and 18:00-06:00 UTC"],
        ["Warn ladder", "4h -> 48h -> 7d, three max"],
        ["Enforcement", "list_only"],
        ["Sample size", "2, confirmed by a second sample"],
        ["Divider", "one per circle, moves to the bottom on each drop"],
    ])
    L += [""]
    L += wrap("The live group is a separate config.yaml. Nothing here is pointed at "
              "it.")
    L += [""]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent.parent / "docs" / "HOW_IT_WORKS.txt"
    text = build()
    out.write_text(text)
    longest = max(len(l) for l in text.splitlines())
    print(f"wrote {out}  lines={len(text.splitlines())}  chars={len(text)}")
    print(f"longest line = {longest} (target <= 78)")
