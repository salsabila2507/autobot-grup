ENTRY_SLASH = "#{pos} / @{tg} {xhandle} / {link}"
ENTRY_SLASH_VIP = "#{pos} / VIP ★ / {link}"
ENTRY_SLASH_ADMIN = "#{pos} / ADMIN 👑 @{tg} {xhandle} / {link}"
ENTRY_SLASH_ADMIN_VIP = "#{pos} / ADMIN 👑 VIP ★ / {link}"
ENTRY_TWO_LINE = "#{pos} @{tg} {xhandle}\n{link}"
ENTRY_TWO_LINE_VIP = "#{pos} VIP ★\n{link}"
ENTRY_TWO_LINE_ADMIN = "#{pos} ADMIN 👑 @{tg} {xhandle}\n{link}"
ENTRY_TWO_LINE_ADMIN_VIP = "#{pos} ADMIN 👑 VIP ★\n{link}"

# Three line format the group owner asked for:
#   ★56
#   @Maskouttt Koyum_1
#   x.com/Koyum_1/status/2110293723489718312
# A badged entry leaves the second line blank. The number stays on the first line even
# for VIP and admin: without it nobody can point at an entry, and /del and /label both
# work by number.
ENTRY_THREE = "★{pos}\n@{tg} {xhandle}\n{link}"
ENTRY_THREE_VIP = "★{pos} Vip ★\n\n{link}"
ENTRY_THREE_ADMIN = "★{pos} Admin ★\n\n{link}"
ENTRY_THREE_ADMIN_VIP = "★{pos} Admin ★ Vip ★\n\n{link}"

ADMIN_BADGE = "👑"
DIVIDER = ("Quick engagement here ([read rules]({rules_link}))\n"
           "{helper}")
DIVIDER_HTML = ('Quick engagement here (<a href="{rules_link}">read rules</a>)\n'
                '{helper}')

# The reminder has to match the circle: telling someone to comment in the likes circle
# sends them to the wrong place.
HELPER_LIKE = "After you like, react ❤️ to the entries above."
HELPER_COMMENT = "After you comment, react ❤️ to the entries above."


def helper_for(kind: str) -> str:
    return HELPER_LIKE if kind == "likes" else HELPER_COMMENT
REJECT_BAD_LINK = "Only X post links in this circle. Send x.com/<user>/status/<id>."
REJECT_ANON = "Turn off anonymous posting to drop. The queue needs your account."
REJECT_DUPLICATE = "You already dropped in this round. Wait for the next divider."
REJECT_VIP_LIMIT = "VIP limit is {limit} drops per rolling 24 hours. Your next drop opens later."
QUARANTINE_NOTE = "This X handle is already bound to another account. An admin will review."
REJECT_BANNED = "This identity is banned from the queue."
EARLY_DROP_ALERT = "{mention} still owes {required} engagements on the previous drop. Engage first, then drop again."
PRISON_WARN_PUBLIC = "{mention} warning {level}/{max} - early drop while obligation open. Mute {duration} until {expires} UTC."
DELETED_NOTE = "#{pos} in {circle} deleted, number stays blank. Reason: {reason} ({at} UTC)"
NOT_FOUND = "Drop #{pos} not found in {circle}."
LABEL_DONE = "#{pos} labeled {label} by {admin} ({at} UTC)"
LABEL_BAD = "Status {status} cannot be labeled."
VIP_SET = "VIP {tg} = {state} ({at} UTC)"
VIP_MISSING = "{tg} has no identity yet (never dropped)."
CLEARED = "{n} obligation(s) of @{tg} marked cleared ({at} UTC)"
CLEARED_DETAIL = "\nClosed {cases} open case(s), cancelled {intents} queued punishment(s). Back to normal."
ADMIN_ONLY = "Admins only."
APPEAL_SAVED = "Appeal filed, case #{case} was flagged for admin review ({at} UTC)."
APPEAL_NO_CASE = "No open defaulter case for your account. Nothing to appeal yet."
APPEAL_RATE_LIMIT = "You already filed an appeal in the last 24 hours. Waiting on an admin."
HEALTH_HEAD = "Window {label}: {start} - {end} UTC"
HEALTH_LINE = "{circle}: next=#{next} | drops24h={drops} | open_obligations={oblig}"
USAGE_DEL = "Usage: /del <position> [reason] (in a circle topic) or /del <circle_key> <position> [reason]"
USAGE_LABEL = "Usage: /label <position> <wrong|defaulter> in a circle, or /label <circle_key> <position> <label>"
USAGE_VIP = "Usage: /addvip <username> to grant, /delvip <username> to revoke"
USAGE_XHANDLE = "Usage: /xhandle <your_x_username>"
USAGE_BIND = "Usage: /bind <tg_user_id> <x_username>"
XHANDLE_BOUND = "Handle @{handle} bound to your account."
BIND_MEMBER_BLOCKED = "Your handle is already set. Changing it is admin only: ask an admin to run /bind <your_id> <username>."
ALT_ALERT = "Alt alert: case #{case}. @{handle} is already bound to another account. New account {new_user} held in quarantine."
BIND_DONE = "@{tg} handle changed {old} -> @{new} ({at} UTC)"
BIND_IN_USE = "Handle @{new} is already bound to another account."
BIND_NO_IDENTITY = "@{tg} has no identity yet, they must drop a link once first."
BIND_MEMBER_BLOCKED = "Changing your X handle is admin only. Ask an admin to run /bind <your_id> <username>."
USAGE_CLEARED = "Usage: /cleared <username|id>, or reply to one of their entries."
DEL_FAIL = "Failed to delete entry #{pos}, check logs."
USAGE_CHECK = "Admins only. Usage: /check <telegram_username|x_username>"
CHECK_NOT_FOUND = "No identity found for {name}. They must drop a link first."
CHECK_NO_DROP = "{name} has no active drop to check yet."
# Never mention how many links are checked, or that the choice is random. Telling
# members the sampling is random invites them to engage only when they expect to be
# sampled, which is exactly the behaviour the checker exists to detect.
CHECK_STARTED = "Checking {name}. This takes a minute or two."

# Only admins can run a check at all, so the result always carries the detail.
CHECK_RESULT_ADMIN = "Check finished for {name}: {verdict}\n{body}"
CHECK_VERDICT_WORDS = {
    "clear": "PASS",
    "violated": "MISS",
    "review": "REVIEW",
    "mixed": "REVIEW",
    "unknown": "NO DATA",
}
CHECK_FAILED = "Check failed: {error}. Nothing was changed."
DROP_SEND_FAILED = "Your drop could not be posted and was rolled back. Nothing was counted. Send it again."
CHECK_AMBIGUOUS = "More than one account matches {name}. Use the exact username."
CHECK_THROTTLED = "Checker busy: {reason}"


ENTRY_FORMATS = ("slash", "two_line", "three")


def entry(entry_format, pos, tg, xhandle, link):
    if entry_format == "slash":
        return ENTRY_SLASH.format(pos=pos, tg=tg, xhandle=xhandle, link=link)
    if entry_format == "three":
        return ENTRY_THREE.format(pos=pos, tg=tg, xhandle=xhandle, link=link)
    return ENTRY_TWO_LINE.format(pos=pos, tg=tg, xhandle=xhandle, link=link)


def entry_admin(entry_format, pos, tg, xhandle, link, vip=False):
    if entry_format == "slash":
        tpl = ENTRY_SLASH_ADMIN_VIP if vip else ENTRY_SLASH_ADMIN
    elif entry_format == "three":
        tpl = ENTRY_THREE_ADMIN_VIP if vip else ENTRY_THREE_ADMIN
    else:
        tpl = ENTRY_TWO_LINE_ADMIN_VIP if vip else ENTRY_TWO_LINE_ADMIN
    return tpl.format(pos=pos, tg=tg, xhandle=xhandle, link=link)


def entry_vip(entry_format, pos, link):
    if entry_format == "slash":
        tpl = ENTRY_SLASH_VIP
    elif entry_format == "three":
        tpl = ENTRY_THREE_VIP
    else:
        tpl = ENTRY_TWO_LINE_VIP
    return tpl.format(pos=pos, link=link)


def divider(rules_link, kind: str = "engagement"):
    return DIVIDER.format(rules_link=rules_link, helper=helper_for(kind))


def divider_html(rules_link, kind: str = "engagement"):
    """Telegram renders the rules link as a clickable 'read rules' at the end."""
    return DIVIDER_HTML.format(rules_link=rules_link, helper=helper_for(kind))


def reject_vip_limit(limit):
    return REJECT_VIP_LIMIT.format(limit=limit)
