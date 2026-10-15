# Link Shield 2.0 🛡️
> **Asynchronous Multi-Tenant Telegram Moderation & Anti-Exploit Engine**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon MTProto](https://img.shields.io/badge/protocol-Telethon%20MTProto-orange.svg)](https://docs.telethon.dev/)
[![Storage](https://img.shields.io/badge/storage-SQLite%20WAL-green.svg)](https://www.sqlite.org/wal.html)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Link Shield 2.0 is an event-driven Telegram moderation engine engineered to protect online communities from modern spam networks, phishing campaigns, and social engineering exploits that evade standard bot filters.

---

## 📌 Problem & Motivation

Modern Telegram spam attacks have evolved beyond simple URL dumps:
1. **Masked Hyperlinks:** Attackers conceal phishing URLs inside Markdown text links (e.g. `[Legitimate Site](https://phishing.link)`).
2. **Channel Forward Exploitation:** Scammers forward posts from private broadcast channels that host forbidden links, bypassing traditional message-level URL filters.
3. **Rogue Channel Mentions:** Malicious actors promote external channels using `@mentions` instead of raw URLs to hijack community members.
4. **Flood & Burst Attacks:** Automated bots overwhelm group chats with rapid multi-message bursts.

**Link Shield 2.0** provides fine-grained, automated defense using low-level Telegram MTProto entity inspection, sliding-window flood control, in-memory LRU caching, and per-group isolated configurations.

---

## 🏗️ System Architecture

```
   Telegram MTProto Network (Telethon Client)
                   │
                   ▼ (events.NewMessage / events.MessageEdited)
    ┌──────────────────────────────────────────────┐
    │     asyncio Event Loop Concurrency           │
    │                                              │
    │  [Stage 1: Identity & Exemption Filter]      │
    │     ├── Check Banned Status                  │
    │     └── Verify Admin (300s TTL Cache) / WL   │
    │                                              │
    │  [Stage 2: Anti-Flood Rate Limiting]         │
    │     └── Sliding Window Tracker -> Auto-Mute  │
    │                                              │
    │  [Stage 3: Channel Forward Inspection]       │
    │     └── PeerChannel ID -> Creator Resolution │
    │                                              │
    │  [Stage 4: Hyperlink & Regex Parser]         │
    │     └── Subdomain Hierarchy vs Domain WL     │
    │                                              │
    │  [Stage 5: Stealth Mention Entity Resolver]  │
    │     └── LRU Entity Cache (Cap: 2,000)        │
    └──────────────────────┬───────────────────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
    [Async Action Dispatcher]    [SQLite Persistence]
    - Self-Deleting Warnings     - WAL Journal Mode
    - 60s Temporary Mute         - Debounced Usage Tracking
    - Dedicated Audit Log        - Per-Group Custom Policies
```

---

## ✨ Core Features

* **Asynchronous Multi-Tenant Architecture:** Every group maintains completely isolated whitelists (domains, channels, users, mentions), custom rate limits, and warning thresholds.
* **Deep Entity Extraction:** Combines regex lookaheads with Telegram's internal message entities (`MessageEntityTextUrl`, `MessageEntityUrl`, `MessageEntityMention`) to catch disguised links.
* **Channel Forward Defense & Auto-Whitelisting:** Inspects `PeerChannel` metadata. Whitelisting a channel dynamically queries the MTProto client for the channel's creator and whitelists them automatically.
* **Sliding-Window Anti-Spam Rate Limiter:** Tracks message frequency per `(chat_id, user_id)`. Exceeding limits triggers an automated 60-second restriction via `ChatBannedRights`.
* **LRU Entity Cache:** Resolves usernames into Telegram entities while avoiding API rate limits (`FloodWaitError`) using an in-memory `OrderedDict` LRU cache capped at 2,000 entities.
* **Tiered Warning Escalation:** Progressive disciplinary system (`/warn`) with configurable thresholds (`/set_warn_threshold`) that auto-escalates to permanent group bans.
* **Optimized Persistence:** SQLite configured with Write-Ahead Logging (`WAL` mode) and normalized synchronization. User activity telemetry is debounced to avoid disk thrashing.
* **Clean Community Experience:** Bot notification messages automatically self-delete after a configurable delay (`/set_delay`), preventing clutter. Supports auto-cleanup of service messages (user joins/leaves).

---

## 🚀 Quickstart & Setup

### Prerequisites
* Python 3.10+
* Telegram API ID & Hash (from [my.telegram.org](https://my.telegram.org))
* Telegram Bot Token (from [@BotFather](https://t.me/BotFather))

### 1. Clone the Repository
```bash
git clone https://github.com/your-username/link-shield-telegram.git
cd link-shield-telegram
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy the example environment file and fill in your credentials:
```bash
cp .env.example .env
```
Edit `.env`:
```env
API_ID=12345678
API_HASH=your_api_hash_here
BOT_TOKEN=1234567890:AAExampleTokenHere
BOT_OWNER_ID=your_numeric_telegram_user_id
BOT_OWNER_USERNAME=your_username
```

### 4. Run the Bot
```bash
python bot.py
```

---

## 🛠️ Command Reference

| Category | Command | Description |
|---|---|---|
| **General** | `/start`, `/help` | Intro and setup instructions (DM only) |
| | `/stats` | Global bot usage metrics |
| | `/commands` | Full administrative command list |
| **Anti-Spam** | `/toggle_antispam` | Enable/disable sliding-window flood protection |
| | `/set_ratelimit <msgs> <secs>` | Configure flood thresholds (e.g., 5 msgs in 3s) |
| **User Moderation** | `/whitelist <user>` | Allow a member to post unrestricted links |
| | `/unwhitelist <user>` | Remove member from whitelist |
| | `/ban <user>`, `/unban <user>`| Manually blacklist or pardon a member |
| **Warnings** | `/warn <user>`, `/unwarn <user>` | Add or remove a disciplinary warning |
| | `/warns <user>` | Check user's current warning count |
| | `/set_warn_threshold <N>` | Set automatic ban threshold (default: 3) |
| **Domain Control**| `/whitelist_domain <domain>` | Allow specific domain (includes subdomains) |
| | `/unwhitelist_domain <domain>`| Remove an approved domain |
| **Channel Defense**| `/whitelist_channel <@channel>`| Permit forwards from channel & whitelist its creator |
| | `/whitelist_mention <@user>` | Whitelist specific channel/bot username mention |
| **Group Settings**| `/set_welcome <text>` | Set customizable welcome greeting with `{user}` |
| | `/toggle_service_cleanup` | Auto-delete join/leave service notifications |
| | `/set_log <@channel>` | Designate a channel for real-time moderation logs |
| | `/set_delay <seconds>` | Configure auto-delete duration for bot responses |

---

## 📄 License
This project is open-source under the [MIT License](LICENSE).
