# Link Shield 2.0 🛡️
> **A Telegram moderation bot that catches phishing links hidden behind trusted text**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon](https://img.shields.io/badge/library-Telethon-orange.svg)](https://docs.telethon.dev/)
[![Storage](https://img.shields.io/badge/storage-SQLite%20WAL-green.svg)](https://www.sqlite.org/wal.html)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 📌 Why I built it

Our international study group was flooded with scammers daily. After too many nights deleting fake accounts by hand, I wrote a simple bot to protect it. Link Shield 2.0 is the rebuilt version.

Spammers use tricks that simple link filters miss:
1. **Masked links:** the text says `python.org`, but the link opens a phishing site.
2. **Channel forwards:** the link is posted in the attacker's channel, then forwarded into the group.
3. **Channel mentions:** promoting `@some_channel` instead of posting a link.
4. **Edit after posting:** a harmless message is edited later to add a link.
5. **Floods:** rapid bursts of messages.

## 🏗️ How it works

Every new **and edited** message runs the same 7 checks. The first one that decides the outcome ends the pipeline.

```
Telegram (Telethon) ── events.NewMessage / events.MessageEdited
        │
        ▼
 1. Banlist ................ banned sender → delete
 2. Admin / whitelist ...... exempt → allow (admin status cached 300 s)
 3. Bot command only ....... allow
 4. Flood limit ............ sliding window → delete + 60 s mute (if enabled)
 5. Channel forward ........ source channel not whitelisted → delete
 6. Links (visible+hidden) . regex on text + URL entities vs domain whitelist
 7. Mentions ............... @name resolves to a channel/group → delete
        │
        ▼
 Self-deleting warnings · optional log channel · SQLite (WAL)
```

## ✨ Features

* **Hidden-link detection:** checks the visible text with a regex *and* the `url` field of `MessageEntityTextUrl` entities, where a masked link's real destination lives. Subdomain-aware: whitelisting `python.org` allows `docs.python.org` but blocks `evilpython.org`.
* **Channel forward defense:** checks the source channel ID in `fwd_from`. `/whitelist_channel` also finds the channel's creator and whitelists them.
* **Edit re-checking:** edited messages run the full pipeline again.
* **Sliding-window rate limiter:** per `(chat_id, user_id)`; default 5 messages per 3 s, then a 60-second mute. Needs the bot's Ban Users admin right.
* **Username cache:** an `OrderedDict` of up to 2,000 resolved usernames, which reduces Telegram lookups. The oldest entries are evicted first.
* **Warnings:** `/warn` with a per-group threshold (default 3). Reaching it adds the user to the bot's banlist, so their messages are deleted.
* **Per-group settings:** whitelists, rate limits, thresholds, delays, and log channel are all stored per group in SQLite (WAL mode). Usage writes are throttled to once per user every 300 s.
* **Clean chats:** bot notices self-delete after a configurable delay, and join/leave messages can be removed automatically.

## ⚠️ Known limitations

* The text regex recognizes about 30 domain endings. Others are caught only if Telegram tags them as links.
* URL buttons on inline-bot messages aren't checked yet.
* Cached usernames never expire.
* SQLite calls are synchronous.
* Telethon was archived upstream in February 2026.

## 🐛 Fixes in 2.0

* Owner check now uses the numeric user ID only (display names could be copied).
* Only bare commands skip moderation (previously any message starting with `/`).
* Entity text is extracted with `get_entities_text()`, fixing emoji-shifted offsets.
* Mute times are timezone-aware.

## 🚀 Setup

**Prerequisites:** Python 3.10+, an API ID and hash from [my.telegram.org](https://my.telegram.org), and a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/Aaditya112developer/link-shield-telegram.git
cd link-shield-telegram
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
```env
API_ID=12345678
API_HASH=your_api_hash_here
BOT_TOKEN=1234567890:AAExampleTokenHere
BOT_OWNER_ID=your_numeric_telegram_user_id
```

Run the bot:
```bash
python Link_Shield_Bot.py
```

Add the bot to your group as an admin with **Delete Messages** permission. Add **Ban Users** permission too if you want flood mutes.

## 🛠️ Commands

| Category | Command | Description |
|---|---|---|
| **General** | `/start`, `/help` | Intro and setup (DM only) |
| | `/stats`, `/commands` | Bot statistics, command list |
| **Anti-spam** | `/toggle_antispam` | Turn flood protection on or off |
| | `/set_ratelimit <msgs> <secs>` | Set the flood limit (e.g. 5 in 3 s) |
| **Users** | `/whitelist`, `/unwhitelist <user>` | Exempt a user from all checks |
| | `/ban`, `/unban <user>` | Delete all future messages from a user |
| **Warnings** | `/warn`, `/unwarn <user>` | Add or remove a warning |
| | `/warns <user>` | Show a user's warning count |
| | `/set_warn_threshold <N>` | Warnings before banlisting (default 3) |
| **Domains** | `/whitelist_domain`, `/unwhitelist_domain <domain>` | Allow a domain and its subdomains |
| **Channels** | `/whitelist_channel`, `/unwhitelist_channel <@channel>` | Allow forwards and whitelist the creator |
| **Mentions** | `/whitelist_mention`, `/unwhitelist_mention <@name>` | Allow mentions of a channel or group |
| **Group** | `/set_welcome <text>`, `/clear_welcome` | Welcome message (`{user}`, `{name}`) |
| | `/toggle_service_cleanup` | Auto-delete join/leave messages |
| | `/set_log <@channel>`, `/clear_log` | Moderation log channel |
| | `/set_delay <seconds>` | Auto-delete delay for bot messages |
| **Lists** | `/show_whitelisted_users`, `/show_banned_users`, `/show_whitelisted_domains`, `/show_whitelisted_channels`, `/show_whitelisted_mentions` | View current settings |

## 📄 License
MIT. See [LICENSE](LICENSE).
