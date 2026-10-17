# HOTTI Engine

Engagement queue engine for Telegram groups. Numbers drops, verifies engagement from X, raises defaulter cases, and drives MissRose for warnings.

**One repo, many groups.** Clone this repository for each group you want to run the bot in. Each clone is completely independent -- its own `config.yaml`, its own database, its own secrets. Nothing is shared between clones.

---

## 1. How it works

A member drops an X link into a circle topic. The bot deletes their raw message, reposts it as a numbered entry, and puts a rules divider underneath.

Then the checker verifies whether the member actually engaged with the links above them:

| Circle | Required | What the member owes |
|---|---|---|
| `eng_blue` | 30 | Heart on the 30 entries above **and** a comment on each of those X posts |
| `like20` | 20 | Heart on the 20 entries above only. X is never opened |

Only one emoji counts: **U+2764 (heart)**. Anything else is treated as no reaction.

An admin runs `/check <name>` -> the bot opens 2 random links from that member's window, reads every reply in a real browser, and returns **PASS**, **MISS**, or **REVIEW**. A MISS opens a case. The scheduler escalates any case still open after the enforce deadline.

See `docs/HOW_IT_WORKS.md` for the full document, including why likes cannot be checked and the tradeoffs of the sampling approach.

---

## 2. Quick start (per group)

```bash
# 1. Clone
git clone https://github.com/salsabila2507/autobot-grup.git
cd autobot-grup

# 2. Install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp config.example.yaml config.yaml
$EDITOR config.yaml                  # group id, topic ids, admin ids

# 4. Secrets
mkdir -p ~/.secrets/groupname
# put bot_token.txt, userbot_session.txt, x_cookies/header.txt there
chmod 600 ~/.secrets/groupname/*

# 5. Check
python -m services.preflight

# 6. Run (in three separate terminals)
python -m services.queue_bot
python -m services.scheduler
python -m services.userbot
```

---

## 3. Configuration

Copy `config.example.yaml` to `config.yaml` and fill in:

```yaml
app:
  api_id: 0              # from https://my.telegram.org
  api_hash: "REPLACE_ME" # from https://my.telegram.org

groups:
  - key: hotti
    main_group: -1003946784525      # your group id
    mod_group: -1003946784525       # same as main (or a separate mod group)
    admin_user_ids: [6438019225]    # Telegram user ids of admins
    mod_bot_username: MissRose_bot
    topics:
      prison: 17                    # public warnings topic
      warnings: 17
      weak_reply: 18
      rules: 19
    circles:
      - key: eng_blue
        topic_id: 15
        kind: engagement
        required: 30
        entry_format: three
        max_drops_per_round: 0
      - key: like20
        topic_id: 16
        kind: likes
        required: 20
        entry_format: three
        max_drops_per_round: 0

rules:
  obligation_required: 30
  vip_drops_per_24h: 4
  miss_threshold: 2
  grace_minutes: 15
  enforce_deadline_minutes: 15
  like_sla_minutes: 10
  public_warnings: true
  warn_ladder: {1: "4h", 2: "48h", 3: "7d"}
  max_warnings: 3

rounds:
  mode: fixed
  schedule_utc: ["06:00-18:00", "18:00-06:00"]

enforcement:
  mode: list_only        # list_only | list_then_auto | auto

secrets:
  queue_bot_token_file: /path/to/secrets/bot_token.txt
  userbot_session_file: /path/to/secrets/userbot_session.txt
  cookie_dir: /path/to/secrets/x_cookies

x_reader:
  cookie_file: /path/to/secrets/x_cookies/header.txt
  sample_size: 2
  confirm_sample: true
  browser_max_scrolls: 60
  browser_idle_rounds: 4
  browser_pause: 1.0
```

All paths are relative to where you run the services. Secrets must be `chmod 600`.

---

## 4. Commands

### Member commands

| Command | Effect |
|---|---|
| drop a link in a circle topic | binds the X handle on the first drop, then queues |
| `/xhandle <x_username>` | binds your handle once, before you have ever dropped |
| `/appeal <reason>` | files an appeal against a case that is still open |

### Admin commands

All answered in the **PRISON** topic. A command typed inside a circle topic is deleted.

| Command | Effect |
|---|---|
| `/check <username or id>` | checks two random links from that member's window |
| `/cleared <username or id>` | full reset: clears debt, closes open cases, cancels queued punishments |
| `/bind <tg_id> <x_username>` | changes an existing handle |
| `/del <circle_key> <position>` | deletes a wrong entry, grants a drop credit |
| `/label <circle_key> <position> <wrong or defaulter>` | labels an entry |
| `/addvip <username>` / `/delvip <username>` | grants or revokes VIP |
| `/shift list, set, del` | admin coverage windows |
| `/health` | service heartbeats and open case count |
| `/approve_alt <id>` / `/reject_alt <id>` | decides an alt handle case |

---

## 5. Enforcement modes

| Mode | Behaviour |
|---|---|
| `list_only` | checks and records everything, **sends nothing** to MissRose |
| `list_then_auto` | fires the ladder after the enforce deadline, on a card nobody touched |
| `auto` | fires immediately |

`list_only` is the launch value: it exercises the reader, entries, reactions and the case pipeline, with no way to mute or ban anyone by mistake.

---

## 6. Testing

```bash
python -m pytest tests/ -q          # 350+ offline tests, no Telegram
python -m sim.sandbox_e2e          # full pipeline rehearsal, no accounts
python -m sim.simulate_rounds --seeds 1000
```

The sandbox rehearsal runs the whole pipeline against a throwaway database, a fake Telegram and a fake reader. It is part of the test suite.

---

## 7. Repository structure

```
engine/          pure logic: sampling, engagement, cases, identity
services/        daemon processes: queue_bot, scheduler, userbot
sim/             offline simulation and the sandbox rehearsal
tests/           offline test suite
docs/            HOW_IT_WORKS.md (full document for the group owner)
deploy/          systemd units and install script
config.example.yaml   placeholder configuration, copy to config.yaml
.env.example          placeholder environment file
```

---

## 8. Notes

- **Backups**: `sqlite3 data/engine.sqlite ".backup 'backup.db'"`, never a raw file copy of a live database.
- **Cookies**: refresh every 2 to 4 weeks. Replace the proxy on a 226 or repeated 403.
- **The reader never POSTs, likes, follows or replies.** Read only.
- **Likes cannot be checked.** Only replies are enumerable from the X page. See section 6 of `docs/HOW_IT_WORKS.md`.

---

## 9. License

MIT. See `LICENSE`.
