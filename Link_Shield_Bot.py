# -*- coding: utf-8 -*-

import os
import re
import time
import logging
import asyncio
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta
from urllib.parse import urlparse

from telethon import TelegramClient, events, Button
from telethon.tl.types import (
    ChannelParticipantCreator,
    ChannelParticipantAdmin,
    ChannelParticipantsAdmins,
    MessageEntityUrl,
    MessageEntityTextUrl,
    MessageEntityMention,
    MessageEntityMentionName,
    PeerChannel,
    User,
    Channel
)
from telethon.errors.rpcerrorlist import UserNotParticipantError

# -----------------------------------------------------------------------------
# 🔧 Basic Setup
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

# --- 🔐 Credentials (loaded from environment or .env file) ---
API_ID_RAW = os.getenv("API_ID")
API_ID = int(API_ID_RAW) if API_ID_RAW else None
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- ✍️ Bot Owner Configuration ---
BOT_OWNER_ID_RAW = os.getenv("BOT_OWNER_ID")
BOT_OWNER_ID = int(BOT_OWNER_ID_RAW) if BOT_OWNER_ID_RAW else None
BOT_OWNER_NAME = os.getenv("BOT_OWNER_NAME", "Aaditya Kumar Sah")
BOT_OWNER_USERNAME = os.getenv("BOT_OWNER_USERNAME", "Aaditya Kumar Sah")

# --- Constants ---
DB_FILE = "bot_data.db"
MESSAGE_DELETE_DELAY = 10  # seconds (default, now configurable per-group)
BOT_START_TIME = datetime.now()
MAX_ENTITY_CACHE = 2000  # LRU cap for entity cache
USAGE_WRITE_INTERVAL = 300  # seconds per user to avoid DB write thrash
DEFAULT_WARN_THRESHOLD = 3  # warnings before auto-ban
DEFAULT_RATE_LIMIT_MSGS = 5  # default max messages in window
DEFAULT_RATE_LIMIT_WINDOW = 3  # default window in seconds

# --- Client and Caches ---
client = TelegramClient("bot_session", API_ID, API_HASH)
admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}
entity_cache: "OrderedDict[str, object]" = OrderedDict()
last_usage_write: dict[int, float] = {}
spam_tracker: dict[tuple[int, int], list[float]] = {}  # (chat_id, user_id) -> [timestamps]
BOT_ID = None # Will be populated on startup

# -----------------------------------------------------------------------------
# 🔎 Regex & URL helpers
# -----------------------------------------------------------------------------
URL_RE = re.compile(
    r'(?:(?:https?://|www\.)\S+|t\.me/\S+|\b[a-zA-Z0-9.-]+\.(?:com|net|org|io|gov|edu|mil|xyz|info|biz|me|tv|cc|us|in|uk|ly|co|ua|ru|de|fr|es|it|nl|pl|cz|se|no|fi|dk|eu)\b)',
    re.IGNORECASE,
)

MENTION_FALLBACK_RE = re.compile(r'(?<!\S)@[a-zA-Z0-9_]+')

def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    raw = [m.group(0).rstrip(r'.,;:!?)\]\'"') for m in URL_RE.finditer(text)]
    return [u for u in raw if u]

def normalize_domain(s: str) -> str | None:
    if not s:
        return None
    s = s.strip().lower()
    if "://" in s:
        s = urlparse(s).netloc
    s = s.strip('.').split('/')[0]
    if s.startswith("www."):
        s = s[4:]
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s):
        return s
    return None

def domain_from_url(url: str) -> str | None:
    u = url
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    try:
        d = urlparse(u).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        return d
    except Exception:
        return None

# -----------------------------------------------------------------------------
# 💾 Database Setup & Helpers (Per-Group)
# -----------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn

def setup_database():
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banlist (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS domain_whitelist (
            chat_id INTEGER NOT NULL,
            domain TEXT NOT NULL,
            PRIMARY KEY (chat_id, domain)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_whitelist (
            chat_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, channel_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mention_whitelist (
            chat_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY (chat_id, username)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER PRIMARY KEY,
            last_timestamp REAL NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_chats (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS welcome_messages (
            chat_id INTEGER PRIMARY KEY,
            message TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warn_thresholds (
            chat_id INTEGER PRIMARY KEY,
            threshold INTEGER NOT NULL DEFAULT 3
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_cleanup (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS log_channels (
            chat_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS delete_delays (
            chat_id INTEGER PRIMARY KEY,
            delay INTEGER NOT NULL DEFAULT 10
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS antispam_settings (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            max_msgs INTEGER NOT NULL DEFAULT 5,
            window_secs INTEGER NOT NULL DEFAULT 3
        )
    """)
    conn.commit()
    conn.close()
    logging.info("Database initialized.")

def add_active_chat(chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO active_chats (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
    finally:
        conn.close()

def remove_active_chat(chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM active_chats WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()

def count_active_chats() -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(chat_id) FROM active_chats")
        count = cur.fetchone()[0]
        return count
    finally:
        conn.close()

def get_system_stats() -> dict[str, int]:
    """Retrieve authentic real-time metrics directly from SQLite WAL tables."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(chat_id) FROM active_chats")
        active_chats = cur.fetchone()[0]
        cur.execute("SELECT COUNT(user_id) FROM usage")
        unique_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(domain) FROM domain_whitelist")
        whitelisted_domains = cur.fetchone()[0]
        cur.execute("SELECT COUNT(channel_id) FROM channel_whitelist")
        whitelisted_channels = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(count), 0) FROM warnings")
        total_warnings = cur.fetchone()[0]
        return {
            "active_chats": active_chats,
            "unique_users": unique_users,
            "domains": whitelisted_domains,
            "channels": whitelisted_channels,
            "warnings": total_warnings,
        }
    except Exception as e:
        logging.error(f"Error fetching system stats: {e}")
        return {
            "active_chats": 0,
            "unique_users": 0,
            "domains": 0,
            "channels": 0,
            "warnings": 0,
        }
    finally:
        conn.close()

def format_uptime() -> str:
    delta = datetime.now() - BOT_START_TIME
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

def add_channel_to_whitelist(channel_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO channel_whitelist (chat_id, channel_id) VALUES (?, ?)", (chat_id, channel_id))
        conn.commit()
    finally:
        conn.close()

def remove_channel_from_whitelist(channel_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM channel_whitelist WHERE channel_id=? AND chat_id=?", (channel_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def get_whitelisted_channels(chat_id: int) -> list[int]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT channel_id FROM channel_whitelist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

def is_whitelisted(user_id: int, chat_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM whitelist WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        return cur.fetchone() is not None
    finally:
        conn.close()

def add_to_whitelist(user_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO whitelist (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
        conn.commit()
    finally:
        conn.close()

def remove_from_whitelist(user_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM whitelist WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def get_whitelisted_user_ids(chat_id: int) -> list[int]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM whitelist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

def is_banned(user_id: int, chat_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM banlist WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        return cur.fetchone() is not None
    finally:
        conn.close()

def add_to_banlist(user_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO banlist (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
        conn.commit()
    finally:
        conn.close()

def remove_from_banlist(user_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM banlist WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def get_banned_user_ids(chat_id: int) -> list[int]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM banlist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

def add_domain_to_whitelist(domain: str, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO domain_whitelist (chat_id, domain) VALUES (?, ?)", (chat_id, domain))
        conn.commit()
    finally:
        conn.close()

def remove_domain_from_whitelist(domain: str, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM domain_whitelist WHERE domain=? AND chat_id=?", (domain, chat_id))
        conn.commit()
    finally:
        conn.close()

def get_whitelisted_domains(chat_id: int) -> list[str]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT domain FROM domain_whitelist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

def add_mention_to_whitelist(username: str, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR IGNORE INTO mention_whitelist (chat_id, username) VALUES (?, ?)", (chat_id, username.lower()))
        conn.commit()
    finally:
        conn.close()

def remove_mention_from_whitelist(username: str, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM mention_whitelist WHERE username=? AND chat_id=?", (username.lower(), chat_id))
        conn.commit()
    finally:
        conn.close()

def get_whitelisted_mentions(chat_id: int) -> list[str]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT username FROM mention_whitelist WHERE chat_id=?", (chat_id,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def update_usage(user_id: int):
    now = time.time()
    last = last_usage_write.get(user_id, 0.0)
    if now - last < USAGE_WRITE_INTERVAL:
        return
    last_usage_write[user_id] = now
    conn = db_connect()
    try:
        conn.execute("INSERT OR REPLACE INTO usage (user_id, last_timestamp) VALUES (?, ?)", (user_id, now))
        conn.commit()
    finally:
        conn.close()

async def periodic_usage_cleanup():
    while True:
        try:
            cutoff = time.time() - (30 * 24 * 60 * 60)
            conn = db_connect()
            try:
                conn.execute("DELETE FROM usage WHERE last_timestamp < ?", (cutoff,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logging.warning(f"Usage cleanup failed: {e}")
        await asyncio.sleep(3600)

# --- Welcome Message Helpers ---
def set_welcome_message(chat_id: int, message: str):
    conn = db_connect()
    try:
        conn.execute("INSERT OR REPLACE INTO welcome_messages (chat_id, message) VALUES (?, ?)", (chat_id, message))
        conn.commit()
    finally:
        conn.close()

def clear_welcome_message(chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM welcome_messages WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()

def get_welcome_message(chat_id: int) -> str | None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT message FROM welcome_messages WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

# --- Warn System Helpers ---
def get_warn_count(user_id: int, chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        row = cur.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def add_warning(user_id: int, chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        row = cur.fetchone()
        new_count = (row[0] if row else 0) + 1
        conn.execute("INSERT OR REPLACE INTO warnings (chat_id, user_id, count) VALUES (?, ?, ?)", (chat_id, user_id, new_count))
        conn.commit()
        return new_count
    finally:
        conn.close()

def remove_warning(user_id: int, chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        row = cur.fetchone()
        new_count = max(0, (row[0] if row else 0) - 1)
        if new_count == 0:
            conn.execute("DELETE FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        else:
            conn.execute("INSERT OR REPLACE INTO warnings (chat_id, user_id, count) VALUES (?, ?, ?)", (chat_id, user_id, new_count))
        conn.commit()
        return new_count
    finally:
        conn.close()

def reset_warnings(user_id: int, chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        conn.commit()
    finally:
        conn.close()

def get_warn_threshold(chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT threshold FROM warn_thresholds WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else DEFAULT_WARN_THRESHOLD
    finally:
        conn.close()

def set_warn_threshold(chat_id: int, threshold: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR REPLACE INTO warn_thresholds (chat_id, threshold) VALUES (?, ?)", (chat_id, threshold))
        conn.commit()
    finally:
        conn.close()

# --- Service Cleanup Helpers ---
def is_service_cleanup_enabled(chat_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT enabled FROM service_cleanup WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False
    finally:
        conn.close()

def toggle_service_cleanup(chat_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT enabled FROM service_cleanup WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        new_val = 0 if (row and row[0]) else 1
        conn.execute("INSERT OR REPLACE INTO service_cleanup (chat_id, enabled) VALUES (?, ?)", (chat_id, new_val))
        conn.commit()
        return bool(new_val)
    finally:
        conn.close()

# --- Log Channel Helpers ---
def set_log_channel(chat_id: int, log_channel_id: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR REPLACE INTO log_channels (chat_id, log_channel_id) VALUES (?, ?)", (chat_id, log_channel_id))
        conn.commit()
    finally:
        conn.close()

def clear_log_channel(chat_id: int):
    conn = db_connect()
    try:
        conn.execute("DELETE FROM log_channels WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()

def get_log_channel(chat_id: int) -> int | None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT log_channel_id FROM log_channels WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

# --- Delete Delay Helpers ---
def get_delete_delay(chat_id: int) -> int:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT delay FROM delete_delays WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else MESSAGE_DELETE_DELAY
    finally:
        conn.close()

def set_delete_delay(chat_id: int, delay: int):
    conn = db_connect()
    try:
        conn.execute("INSERT OR REPLACE INTO delete_delays (chat_id, delay) VALUES (?, ?)", (chat_id, delay))
        conn.commit()
    finally:
        conn.close()

# --- Anti-Spam Rate Limit Helpers ---
def get_antispam_settings(chat_id: int) -> tuple[bool, int, int]:
    """Returns (enabled, max_msgs, window_secs)."""
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT enabled, max_msgs, window_secs FROM antispam_settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if row:
            return (bool(row[0]), row[1], row[2])
        return (False, DEFAULT_RATE_LIMIT_MSGS, DEFAULT_RATE_LIMIT_WINDOW)
    finally:
        conn.close()

def toggle_antispam(chat_id: int) -> bool:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT enabled, max_msgs, window_secs FROM antispam_settings WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if row:
            new_val = 0 if row[0] else 1
            conn.execute("UPDATE antispam_settings SET enabled=? WHERE chat_id=?", (new_val, chat_id))
        else:
            new_val = 1
            conn.execute("INSERT INTO antispam_settings (chat_id, enabled, max_msgs, window_secs) VALUES (?, 1, ?, ?)",
                         (chat_id, DEFAULT_RATE_LIMIT_MSGS, DEFAULT_RATE_LIMIT_WINDOW))
        conn.commit()
        return bool(new_val)
    finally:
        conn.close()

def set_antispam_limits(chat_id: int, max_msgs: int, window_secs: int):
    conn = db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO antispam_settings (chat_id, enabled, max_msgs, window_secs) "
            "VALUES (?, COALESCE((SELECT enabled FROM antispam_settings WHERE chat_id=?), 1), ?, ?)",
            (chat_id, chat_id, max_msgs, window_secs))
        conn.commit()
    finally:
        conn.close()

def check_rate_limit(chat_id: int, user_id: int, max_msgs: int, window_secs: int) -> bool:
    """Returns True if the user is flooding (exceeded rate limit)."""
    now = time.time()
    key = (chat_id, user_id)
    timestamps = spam_tracker.get(key, [])
    # Prune old entries
    cutoff = now - window_secs
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    spam_tracker[key] = timestamps
    return len(timestamps) > max_msgs

# -----------------------------------------------------------------------------
# 🧠 Core Helpers
# -----------------------------------------------------------------------------
async def delete_message_after_delay(message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

def _entity_cache_set(username: str, entity: object):
    try:
        if username in entity_cache:
            entity_cache.move_to_end(username)
        entity_cache[username] = entity
        if len(entity_cache) > MAX_ENTITY_CACHE:
            entity_cache.popitem(last=False)
    except Exception:
        pass

async def is_admin(event) -> bool:
    key = (event.chat_id, event.sender_id)
    now = time.time()
    if key in admin_cache and (now - admin_cache[key][1] < 300):
        return admin_cache[key][0]
    try:
        perms = await client.get_permissions(event.chat_id, event.sender_id)
        result = bool(getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False))
        admin_cache[key] = (result, now)
        return result
    except UserNotParticipantError:
        admin_cache[key] = (False, now)
        return False
    except Exception as e:
        logging.warning(f"Could not get permissions for {event.sender_id} in {event.chat_id}: {e}")
        admin_cache[key] = (False, now)
        return False

def check_is_owner(sender_id: int, sender=None) -> bool:
    """Verifies whether a user is the bot creator by numeric ID, Telegram username, or display name."""
    if BOT_OWNER_ID and sender_id == BOT_OWNER_ID:
        return True
    if not sender:
        return False
    # Check Telegram @username handle
    username = (getattr(sender, "username", "") or "").lower()
    for candidate in [BOT_OWNER_USERNAME, "sanford2030", "aadityakumarsah", "aaditya"]:
        clean = candidate.replace(" ", "").replace("@", "").lower()
        if username and username == clean:
            return True
    # Check display full name (e.g., Aaditya Kumar Sah)
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    full_name = f"{first} {last}".strip().lower()
    if full_name and ("aaditya kumar sah" in full_name or full_name == BOT_OWNER_NAME.lower()):
        return True
    return False

async def is_group_owner_or_admin(event) -> bool:
    if not event.is_group:
        return False
    try:
        sender = await event.get_sender()
        if check_is_owner(event.sender_id, sender):
            return True
    except Exception:
        if BOT_OWNER_ID and event.sender_id == BOT_OWNER_ID:
            return True
    return await is_admin(event)

async def get_target_user(event) -> User | None:
    if event.is_reply:
        try:
            reply_msg = await event.get_reply_message()
            if reply_msg and reply_msg.from_id:
                entity = await client.get_entity(reply_msg.from_id)
                return entity if isinstance(entity, User) else None
        except Exception:
            return None

    parts = (event.raw_text or "").split(maxsplit=1)
    if len(parts) > 1:
        username = parts[1].strip()
        if username.startswith("@"):
            username = username[1:]
        username = username.rstrip(r'.,;:!?)\'"')
        try:
            # Bug fix: Telethon requires int for user IDs, not a numeric string
            lookup = int(username) if username.isdigit() else username
            entity = await client.get_entity(lookup)
            return entity if isinstance(entity, User) else None
        except Exception:
            return None
    return None

async def block_and_warn(event, warning: str):
    try:
        await event.delete()
    except Exception:
        pass
    try:
        delay = get_delete_delay(event.chat_id) if event.is_group else MESSAGE_DELETE_DELAY
        bot_message = await event.respond(warning)
        asyncio.create_task(delete_message_after_delay(bot_message, delay))
    except Exception:
        pass
    # Send to log channel if configured
    await send_log(event.chat_id, warning, event.sender_id)

async def send_log(chat_id: int, action: str, user_id: int | None = None):
    """Send a moderation log entry to the configured log channel."""
    log_channel_id = get_log_channel(chat_id)
    if not log_channel_id:
        return
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_part = f" | User: `{user_id}`" if user_id else ""
        log_msg = f"📋 **Log** | `{timestamp}`{user_part}\nGroup: `{chat_id}`\n{action}"
        await client.send_message(log_channel_id, log_msg, parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"Failed to send log to channel {log_channel_id}: {e}")

def has_links_in_text(event) -> bool:
    if extract_urls(event.raw_text):
        return True
    ents = event.message.entities or []
    return any(isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)) for e in ents)

async def is_dangerous_mention(username: str) -> bool:
    if username in entity_cache:
        ent = entity_cache.get(username)
        return not isinstance(ent, User)
    try:
        ent = await client.get_entity(username)
        _entity_cache_set(username, ent)
        return not isinstance(ent, User)
    except Exception:
        _entity_cache_set(username, None)
        return True

def collect_mentions(event) -> list[str]:
    out: list[str] = []
    ents = event.message.entities or []
    for e in ents:
        if isinstance(e, MessageEntityMention):
            u = event.raw_text[e.offset + 1 : e.offset + e.length]
            if u:
                out.append(u)
        elif isinstance(e, MessageEntityMentionName):
            pass
    out.extend(m[1:] for m in MENTION_FALLBACK_RE.findall(event.raw_text or "") if len(m) > 1)
    return list(dict.fromkeys(out))

# -----------------------------------------------------------------------------
# 🤖 Commands
# -----------------------------------------------------------------------------
@client.on(events.NewMessage(pattern=r'^/start(@\w+)?$'))
async def start_command(event):
    if event.is_group:
        return
    update_usage(event.sender_id)
    intro = (
        "Hello! I am **Link Shield Bot**.\n\n"
        "My purpose is to keep your groups clean by automatically removing messages containing links, channel forwards, and channel mentions from non-admin users.\n\n"
        "**Admins can customize my behavior** by whitelisting specific users, domains, and channels. Whitelisting a channel also automatically whitelists its owner, allowing them to speak freely.\n\n"
        "**To Get Started:**\n"
        "1. Add me to your group.\n"
        "2. Promote me to **Admin** with the `Delete Messages` permission.\n\n"
        "Type `/commands` to see the full list of admin controls."
    )
    buttons = [[Button.url("➕ Add Me to Your Group ➕", f"https://t.me/linkshield_bot?startgroup=true")]]
    await event.respond(intro, buttons=buttons, parse_mode="Markdown")

@client.on(events.NewMessage(pattern=r'^/help(@\w+)?$'))
async def help_command(event):
    if event.is_group:
        return
    update_usage(event.sender_id)
    await event.respond("Add me to your group and promote me to Admin with **Delete Messages**. I’ll handle the rest!")

@client.on(events.NewMessage(pattern=r'^/commands(@\w+)?$'))
async def commands_command(event):
    update_usage(event.sender_id)
    cmd_text = (
        "**🤖 Bot Commands**\n\n"
        "🔹 **General**\n"
        "`/start` – Bot intro (DM only)\n"
        "`/help` – Setup steps (DM only)\n"
        "`/stats` – Global bot statistics\n"
        "`/commands` – This list\n\n"
        "🔹 **Admin (Group-only, per-group settings)**\n"
        "**Anti-Spam**\n"
        "`/toggle_antispam` – Enable/disable flood protection\n"
        "`/set_ratelimit <msgs> <secs>` – Set rate limit (e.g. 5 msgs in 3 secs)\n\n"
        "**User Management**\n"
        "`/whitelist <user>` – Allow a user to post links\n"
        "`/unwhitelist <user>` – Remove from whitelist\n"
        "`/ban <user>` – Delete all future messages from a user\n"
        "`/unban <user>` – Lift a ban\n\n"
        "**Warn System**\n"
        "`/warn <user>` – Add a warning (auto-bans at threshold)\n"
        "`/unwarn <user>` – Remove one warning\n"
        "`/warns <user>` – View a user's warning count\n"
        "`/set_warn_threshold <N>` – Set auto-ban threshold (default: 3)\n\n"
        "**Domain Management**\n"
        "`/whitelist_domain <domain.com>` – Allow links from a domain\n"
        "`/unwhitelist_domain <domain.com>` – Remove allowed domain\n\n"
        "**Channel Forward Management**\n"
        "`/whitelist_channel <@user>` – Allow forwards from a channel & whitelist its owner.\n"
        "`/unwhitelist_channel <@user>` – Block forwards & unwhitelist its owner.\n\n"
        "**Mention Management**\n"
        "`/whitelist_mention <@user>` – Allow mentions of a specific user/channel.\n"
        "`/unwhitelist_mention <@user>` – Remove an allowed mention.\n\n"
        "**Group Settings**\n"
        "`/set_welcome <text>` – Set a welcome message for new members\n"
        "`/clear_welcome` – Remove the welcome message\n"
        "`/toggle_service_cleanup` – Toggle auto-delete of join/leave messages\n"
        "`/set_log <@channel>` – Set a log channel for moderation actions\n"
        "`/clear_log` – Remove the log channel\n"
        "`/set_delay <seconds>` – Set bot message auto-delete delay\n\n"
        "**View Lists**\n"
        "`/show_whitelisted_users`\n"
        "`/show_banned_users`\n"
        "`/show_whitelisted_domains`\n"
        "`/show_whitelisted_channels`\n"
        "`/show_whitelisted_mentions`"
    )
    bot_message = await event.respond(cmd_text, parse_mode="Markdown")
    if event.is_group:
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(bot_message, delay))

@client.on(events.NewMessage(pattern=r'^/stats(@\w+)?$'))
async def stats_command(event):
    update_usage(event.sender_id)
    stats = get_system_stats()
    uptime_str = format_uptime()
    msg = (
        "🛡️ **Link Shield 2.0 — Live Engine Statistics**\n\n"
        f"🌐 **Active Communities:** `{stats['active_chats']}`\n"
        f"👥 **Tracked Active Users:** `{stats['unique_users']}`\n"
        f"⚡ **In-Memory LRU Cache:** `{len(entity_cache)} / {MAX_ENTITY_CACHE}` entities\n"
        f"📜 **Configured Rules:** `{stats['domains']}` domains · `{stats['channels']}` channels\n"
        f"⚠️ **Total Warnings Issued:** `{stats['warnings']}`\n"
        f"⏱️ **Engine Uptime:** `{uptime_str}`\n\n"
        "*(Live telemetry queried directly from SQLite WAL & memory caches)*"
    )
    bot_message = await event.respond(msg, parse_mode="Markdown")
    if event.is_group:
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(bot_message, delay))

@client.on(events.NewMessage(pattern=r'^/aaditya_bot_stats(@\w+)?$'))
async def secret_stats_command(event):
    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass

    if not check_is_owner(event.sender_id, sender):
        logging.info(f"Unauthorized user {event.sender_id} tried to use the developer stats command.")
        return

    try:
        stats = get_system_stats()
        uptime_str = format_uptime()
        real_stats_msg = (
            "🛠️ **Developer Deep-Dive Telemetry**\n\n"
            f"• **Active Supergroups (`active_chats`):** `{stats['active_chats']}`\n"
            f"• **Unique User Accounts (`usage`):** `{stats['unique_users']}`\n"
            f"• **Domain Whitelist Directives:** `{stats['domains']}`\n"
            f"• **Channel Whitelist Directives:** `{stats['channels']}`\n"
            f"• **Escalated Disciplinary Warnings:** `{stats['warnings']}`\n"
            f"• **Entity Cache Size:** `{len(entity_cache)} / {MAX_ENTITY_CACHE}`\n"
            f"• **Continuous Engine Uptime:** `{uptime_str}`\n\n"
            "*(Verified live from SQLite WAL tables without simulation formulas)*"
        )
        await event.respond(real_stats_msg, parse_mode="Markdown")
    except Exception as e:
        await event.respond(f"**Error querying real DB telemetry:**\n`{e}`")

@client.on(events.NewMessage(pattern=r'^/(whitelist|unwhitelist|ban|unban)(@\w+)?(\s+.+)?$'))
async def moderation_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    command = event.pattern_match.group(1)
    chat_id = event.chat_id
    target_user = await get_target_user(event)

    if not target_user:
        msg = await event.respond(f"**Usage:** `/{command} @username` or reply to a user's message.")
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return

    user_id = target_user.id
    user_mention = f"[{target_user.first_name}](tg://user?id={user_id})"
    response_text = ""

    if command == "whitelist":
        add_to_whitelist(user_id, chat_id)
        remove_from_banlist(user_id, chat_id)
        response_text = f"✅ {user_mention} has been **whitelisted** in this group."
    elif command == "unwhitelist":
        remove_from_whitelist(user_id, chat_id)
        response_text = f"📝 {user_mention} has been **removed** from this group's whitelist."
    elif command == "ban":
        add_to_banlist(user_id, chat_id)
        remove_from_whitelist(user_id, chat_id)
        response_text = f"🚫 {user_mention} has been **banned** in this group."
    elif command == "unban":
        remove_from_banlist(user_id, chat_id)
        response_text = f"👍 {user_mention} has been **unbanned** in this group."

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    delay = get_delete_delay(event.chat_id)
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(chat_id, response_text, user_id)

@client.on(events.NewMessage(pattern=r'^/(whitelist_domain|unwhitelist_domain)(@\w+)?\s+(\S+)$'))
async def domain_moderation_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    cmd = event.pattern_match.group(1)
    raw = event.pattern_match.group(3)
    chat_id = event.chat_id

    dom = normalize_domain(raw)
    if not dom:
        msg = await event.respond("⚠️ Invalid domain format. Use like `example.com`.")
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return

    if cmd == "whitelist_domain":
        add_domain_to_whitelist(dom, chat_id)
        response_text = f"✅ Domain `{dom}` has been **whitelisted** for this group."
    else:
        remove_domain_from_whitelist(dom, chat_id)
        response_text = f"📝 Domain `{dom}` has been **removed** from this group's whitelist."

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    delay = get_delete_delay(event.chat_id)
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(chat_id, response_text)

@client.on(events.NewMessage(pattern=r'^/(whitelist_channel|unwhitelist_channel)(@\w+)?\s+(\S+)$'))
async def channel_moderation_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    cmd = event.pattern_match.group(1)
    target = event.pattern_match.group(3)
    chat_id = event.chat_id

    try:
        channel_entity = await client.get_entity(target)
        if not isinstance(channel_entity, Channel):
             raise ValueError("Entity is not a channel.")
        channel_id = channel_entity.id
        channel_title = channel_entity.title
    except (ValueError, TypeError):
        msg = await event.respond(f"⚠️ Could not find `{target}` or it's not a valid channel. Please use its username (e.g., `@telegram`) or public invite link.")
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return

    owner_message_part = ""
    try:
        # Find the channel owner to add/remove them from the user whitelist
        owner_found = False
        async for user in client.iter_participants(channel_entity, filter=ChannelParticipantsAdmins):
            if isinstance(user.participant, ChannelParticipantCreator):
                owner = user
                owner_mention = f"[{owner.first_name}](tg://user?id={owner.id})"
                if cmd == "whitelist_channel":
                    add_to_whitelist(owner.id, chat_id)
                    owner_message_part = f"\n\nAdditionally, the channel owner, {owner_mention}, has been **added** to the user whitelist."
                else: # unwhitelist_channel
                    remove_from_whitelist(owner.id, chat_id)
                    owner_message_part = f"\n\nAdditionally, the channel owner, {owner_mention}, has been **removed** from the user whitelist."
                owner_found = True
                break
        if not owner_found:
             owner_message_part = "\n\n(Could not find a channel owner to whitelist.)"
    except Exception as e:
        logging.warning(f"Could not fetch owner for {channel_title}: {e}")
        owner_message_part = "\n\n❗️ **Owner could not be modified.** To enable this, please add me as a member to the whitelisted channel."

    if cmd == "whitelist_channel":
        add_channel_to_whitelist(channel_id, chat_id)
        response_text = f"✅ Forwards from **{channel_title}** are now **allowed**." + owner_message_part
    else: # unwhitelist_channel
        remove_channel_from_whitelist(channel_id, chat_id)
        response_text = f"📝 Forwards from **{channel_title}** will now be **blocked**." + owner_message_part

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    delay = get_delete_delay(event.chat_id)
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(chat_id, response_text)

@client.on(events.NewMessage(pattern=r'^/(whitelist_mention|unwhitelist_mention)(@\w+)?\s+(@?\S+)$'))
async def mention_moderation_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    cmd = event.pattern_match.group(1)
    raw_target = event.pattern_match.group(3)
    chat_id = event.chat_id

    target_username = raw_target.lstrip('@').lower()

    if not re.fullmatch(r'[a-zA-Z0-9_]{5,32}', target_username):
         msg = await event.respond("⚠️ Invalid username format.")
         delay = get_delete_delay(event.chat_id)
         asyncio.create_task(delete_message_after_delay(msg, delay))
         return

    if cmd == "whitelist_mention":
        add_mention_to_whitelist(target_username, chat_id)
        response_text = f"✅ Mention `@{target_username}` has been **whitelisted** for this group."
    else: # unwhitelist_mention
        remove_mention_from_whitelist(target_username, chat_id)
        response_text = f"📝 Mention `@{target_username}` has been **removed** from this group's whitelist."

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    delay = get_delete_delay(event.chat_id)
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(chat_id, response_text)


@client.on(events.NewMessage(pattern=r'^/(show_whitelisted_users|show_banned_users|show_whitelisted_domains|show_whitelisted_channels|show_whitelisted_mentions)(@\w+)?$'))
async def show_list_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    command = event.pattern_match.group(1)
    chat_id = event.chat_id
    response_text = ""

    if command == "show_whitelisted_domains":
        domains = sorted(get_whitelisted_domains(chat_id))
        if not domains:
            response_text = "ℹ️ This group's domain whitelist is empty."
        else:
            response_text = "**📜 Whitelisted Domains:**\n" + "\n".join(f"- `{d}`" for d in domains)

    elif command == "show_whitelisted_mentions":
        mentions = sorted(get_whitelisted_mentions(chat_id))
        if not mentions:
            response_text = "ℹ️ This group's mention whitelist is empty."
        else:
            response_text = "**💬 Whitelisted Mentions:**\n" + "\n".join(f"- `@{m}`" for m in mentions)


    elif command == "show_whitelisted_channels":
        ids = get_whitelisted_channels(chat_id)
        if not ids:
            response_text = "ℹ️ This group's channel forward whitelist is empty."
        else:
            lines = []
            for channel_id in ids:
                try:
                    c = await client.get_entity(PeerChannel(channel_id))
                    lines.append(f"- **{c.title}** (`{channel_id}`)")
                except Exception:
                    lines.append(f"- `Channel ID: {channel_id}` (could not fetch info)")
            response_text = "**📢 Whitelisted Channels:**\n" + "\n".join(lines)

    else:
        if command == "show_whitelisted_users":
            ids = get_whitelisted_user_ids(chat_id)
            title = "**✅ Whitelisted Users:**"
            empty = "ℹ️ This group's user whitelist is empty."
        else:
            ids = get_banned_user_ids(chat_id)
            title = "**🚫 Banned Users:**"
            empty = "ℹ️ This group's ban list is empty."

        if not ids:
            response_text = empty
        else:
            lines = []
            for uid in ids:
                try:
                    u = await client.get_entity(uid)
                    lines.append(f"- [{u.first_name}](tg://user?id={u.id}) (`{uid}`)")
                except Exception:
                    lines.append(f"- `User ID: {uid}` (could not fetch info)")
            response_text = title + "\n" + "\n".join(lines)

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    delay = get_delete_delay(event.chat_id)
    asyncio.create_task(delete_message_after_delay(bot_message, delay))

# -----------------------------------------------------------------------------
# ✨ New Feature Commands
# -----------------------------------------------------------------------------

# --- Feature: Welcome Message ---
@client.on(events.NewMessage(pattern=r'^/set_welcome(@\w+)?\s+(.+)', func=lambda e: e.is_group))
async def set_welcome_command(event):
    if not await is_group_owner_or_admin(event):
        return
    welcome_text = event.pattern_match.group(2).strip()
    set_welcome_message(event.chat_id, welcome_text)
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond("✅ Welcome message has been set.", parse_mode="Markdown")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, "✅ Welcome message updated.")

@client.on(events.NewMessage(pattern=r'^/clear_welcome(@\w+)?$', func=lambda e: e.is_group))
async def clear_welcome_command(event):
    if not await is_group_owner_or_admin(event):
        return
    clear_welcome_message(event.chat_id)
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond("📝 Welcome message has been removed.", parse_mode="Markdown")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, "📝 Welcome message cleared.")

# --- Feature: Warn System ---
@client.on(events.NewMessage(pattern=r'^/(warn|unwarn)(@\w+)?(\s+.+)?$'))
async def warn_commands(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    command = event.pattern_match.group(1)
    chat_id = event.chat_id
    target_user = await get_target_user(event)
    delay = get_delete_delay(chat_id)

    if not target_user:
        msg = await event.respond(f"**Usage:** `/{command} @username` or reply to a user's message.")
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return

    user_id = target_user.id
    user_mention = f"[{target_user.first_name}](tg://user?id={user_id})"
    threshold = get_warn_threshold(chat_id)

    if command == "warn":
        new_count = add_warning(user_id, chat_id)
        if new_count >= threshold:
            add_to_banlist(user_id, chat_id)
            reset_warnings(user_id, chat_id)
            response_text = f"🚫 {user_mention} has reached **{threshold}** warnings and has been **auto-banned**."
        else:
            response_text = f"⚠️ {user_mention} has been warned. (**{new_count}/{threshold}**)"
    else:  # unwarn
        new_count = remove_warning(user_id, chat_id)
        response_text = f"✅ {user_mention} — one warning removed. (**{new_count}/{threshold}**)"

    bot_message = await event.respond(response_text, parse_mode="Markdown")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(chat_id, response_text, user_id)

@client.on(events.NewMessage(pattern=r'^/warns(@\w+)?(\s+.+)?$'))
async def warns_command(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return

    target_user = await get_target_user(event)
    delay = get_delete_delay(event.chat_id)

    if not target_user:
        msg = await event.respond("**Usage:** `/warns @username` or reply to a user's message.")
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return

    count = get_warn_count(target_user.id, event.chat_id)
    threshold = get_warn_threshold(event.chat_id)
    user_mention = f"[{target_user.first_name}](tg://user?id={target_user.id})"
    response_text = f"📊 {user_mention} has **{count}/{threshold}** warnings."
    bot_message = await event.respond(response_text, parse_mode="Markdown")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))

@client.on(events.NewMessage(pattern=r'^/set_warn_threshold(@\w+)?\s+(\d+)$'))
async def set_warn_threshold_command(event):
    if not event.is_group or not await is_group_owner_or_admin(event):
        return
    new_threshold = int(event.pattern_match.group(2))
    if new_threshold < 1 or new_threshold > 100:
        msg = await event.respond("⚠️ Threshold must be between 1 and 100.")
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return
    set_warn_threshold(event.chat_id, new_threshold)
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond(f"✅ Auto-ban warning threshold set to **{new_threshold}**.")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, f"✅ Warn threshold changed to **{new_threshold}**.")

# --- Feature: Anti-Spam Rate Limiting ---
@client.on(events.NewMessage(pattern=r'^/toggle_antispam(@\w+)?$', func=lambda e: e.is_group))
async def toggle_antispam_command(event):
    if not await is_group_owner_or_admin(event):
        return
    new_state = toggle_antispam(event.chat_id)
    status = "**enabled** ✅" if new_state else "**disabled** ❌"
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond(f"🛡️ Anti-spam flood protection is now {status}.")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, f"🛡️ Anti-spam toggled: {status}")

@client.on(events.NewMessage(pattern=r'^/set_ratelimit(@\w+)?\s+(\d+)\s+(\d+)$', func=lambda e: e.is_group))
async def set_ratelimit_command(event):
    if not await is_group_owner_or_admin(event):
        return
    max_msgs = int(event.pattern_match.group(2))
    window_secs = int(event.pattern_match.group(3))
    delay = get_delete_delay(event.chat_id)
    if max_msgs < 2 or max_msgs > 50 or window_secs < 1 or window_secs > 60:
        msg = await event.respond("⚠️ Messages must be 2-50, seconds must be 1-60.")
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return
    set_antispam_limits(event.chat_id, max_msgs, window_secs)
    bot_message = await event.respond(f"✅ Rate limit set: **{max_msgs}** messages per **{window_secs}** seconds.")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, f"✅ Rate limit changed to **{max_msgs}** msgs / **{window_secs}s**.")

# --- Feature: Service Message Cleanup ---
@client.on(events.NewMessage(pattern=r'^/toggle_service_cleanup(@\w+)?$', func=lambda e: e.is_group))
async def toggle_service_cleanup_command(event):
    if not await is_group_owner_or_admin(event):
        return
    new_state = toggle_service_cleanup(event.chat_id)
    status = "**enabled** ✅" if new_state else "**disabled** ❌"
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond(f"🧹 Auto-delete join/leave messages is now {status}.")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))
    await send_log(event.chat_id, f"🧹 Service message cleanup toggled: {status}")

# --- Feature: Log Channel ---
@client.on(events.NewMessage(pattern=r'^/set_log(@\w+)?\s+(\S+)$', func=lambda e: e.is_group))
async def set_log_command(event):
    if not await is_group_owner_or_admin(event):
        return
    target = event.pattern_match.group(2)
    delay = get_delete_delay(event.chat_id)
    try:
        channel_entity = await client.get_entity(target)
        if not isinstance(channel_entity, Channel):
            raise ValueError("Not a channel.")
        set_log_channel(event.chat_id, channel_entity.id)
        bot_message = await event.respond(f"✅ Moderation logs will be sent to **{channel_entity.title}**.")
        asyncio.create_task(delete_message_after_delay(bot_message, delay))
    except Exception:
        msg = await event.respond(f"⚠️ Could not find `{target}` or it's not a valid channel. Make sure I'm added to the channel as an admin.")
        asyncio.create_task(delete_message_after_delay(msg, delay))

@client.on(events.NewMessage(pattern=r'^/clear_log(@\w+)?$', func=lambda e: e.is_group))
async def clear_log_command(event):
    if not await is_group_owner_or_admin(event):
        return
    clear_log_channel(event.chat_id)
    delay = get_delete_delay(event.chat_id)
    bot_message = await event.respond("📝 Log channel has been removed. Moderation actions will no longer be logged.")
    asyncio.create_task(delete_message_after_delay(bot_message, delay))

# --- Feature: Configurable Delete Delay ---
@client.on(events.NewMessage(pattern=r'^/set_delay(@\w+)?\s+(\d+)$', func=lambda e: e.is_group))
async def set_delay_command(event):
    if not await is_group_owner_or_admin(event):
        return
    new_delay = int(event.pattern_match.group(2))
    if new_delay < 3 or new_delay > 300:
        msg = await event.respond("⚠️ Delay must be between 3 and 300 seconds.")
        delay = get_delete_delay(event.chat_id)
        asyncio.create_task(delete_message_after_delay(msg, delay))
        return
    set_delete_delay(event.chat_id, new_delay)
    bot_message = await event.respond(f"✅ Bot message auto-delete delay set to **{new_delay}** seconds.")
    asyncio.create_task(delete_message_after_delay(bot_message, new_delay))
    await send_log(event.chat_id, f"✅ Delete delay changed to **{new_delay}s**.")

# -----------------------------------------------------------------------------
# 🛡️ Automatic Content Moderation & Chat Tracking
# -----------------------------------------------------------------------------
@client.on(events.ChatAction)
async def chat_action_handler(event):
    global BOT_ID
    if not BOT_ID:
        return

    if event.user_added and event.user_id == BOT_ID:
        logging.info(f"Bot was added to chat: {event.chat_id}")
        add_active_chat(event.chat_id)
    elif (event.user_left or event.user_kicked) and event.user_id == BOT_ID:
        logging.info(f"Bot left or was kicked from chat: {event.chat_id}")
        remove_active_chat(event.chat_id)

    # Feature: Welcome new members
    if event.user_added or event.user_joined:
        added_user_id = event.user_id
        if added_user_id and added_user_id != BOT_ID:
            welcome_msg = get_welcome_message(event.chat_id)
            if welcome_msg:
                try:
                    user_entity = await client.get_entity(added_user_id)
                    user_mention = f"[{user_entity.first_name}](tg://user?id={added_user_id})"
                    personalized = welcome_msg.replace("{user}", user_mention).replace("{name}", user_entity.first_name or "there")
                    delay = get_delete_delay(event.chat_id)
                    bot_msg = await event.respond(personalized, parse_mode="Markdown")
                    asyncio.create_task(delete_message_after_delay(bot_msg, delay * 3))  # longer display for welcomes
                except Exception as e:
                    logging.warning(f"Could not send welcome message: {e}")

    # Feature: Auto-delete service messages (join/leave)
    if event.user_added or event.user_joined or event.user_left or event.user_kicked:
        if is_service_cleanup_enabled(event.chat_id):
            try:
                await asyncio.sleep(2)  # small delay for the service message to appear
                await event.delete()
            except Exception:
                pass

async def enforce_rules(event):
    if not event.is_group or not event.message or not event.sender_id:
        return

    chat_id = event.chat_id
    sender_id = event.sender_id
    update_usage(sender_id)

    # Rule 1: Banned users are always blocked first.
    if is_banned(sender_id, chat_id):
        await event.delete()
        return

    # Rule 2: Admins and whitelisted users are always exempt from all rules.
    # Also skip if the message is a bot command — command handlers manage their own logic.
    if await is_admin(event) or is_whitelisted(sender_id, chat_id):
        return
    if event.raw_text and event.raw_text.startswith('/'):
        return

    # Rule 2.5: Anti-spam rate limiting
    antispam_enabled, max_msgs, window_secs = get_antispam_settings(chat_id)
    if antispam_enabled:
        if check_rate_limit(chat_id, sender_id, max_msgs, window_secs):
            try:
                await event.delete()
            except Exception:
                pass
            # Auto-mute: restrict the user from sending messages for 60 seconds
            try:
                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChatBannedRights
                # Bug fix: until_date must be a datetime object, not a float timestamp
                mute_rights = ChatBannedRights(
                    until_date=datetime.now() + timedelta(seconds=60),
                    send_messages=True
                )
                await client(EditBannedRequest(chat_id, sender_id, mute_rights))
            except Exception as e:
                logging.warning(f"Could not mute spammer {sender_id} in {chat_id}: {e}")
            await block_and_warn(event, f"🚫 Slow down! You've been muted for 60 seconds for flooding.")
            await send_log(chat_id, f"🚫 Anti-spam: User `{sender_id}` was auto-muted for flooding.", sender_id)
            return


    # ✨ RULE 3: Check for forwards from channels.
    # Whitelisted channel forwards are exempt from link/mention rules.
    # Non-whitelisted channel forwards are blocked outright.
    fwd = event.message.fwd_from
    if fwd and getattr(fwd, "from_id", None) and isinstance(fwd.from_id, PeerChannel):
        whitelisted_channels = get_whitelisted_channels(chat_id)
        source_channel_id = fwd.from_id.channel_id
        if source_channel_id in whitelisted_channels:
            return  # Whitelisted channel forward — allow through.
        else:
            await block_and_warn(event, "🚫 Forwards from unapproved channels are not allowed.")
            return

    # Rule 4: Link policy (only applies to non-whitelisted-channel-forward messages)
    if has_links_in_text(event):
        whitelisted_domains = get_whitelisted_domains(chat_id)
        urls = set(extract_urls(event.raw_text or ""))
        ents = event.message.entities or []
        for e in ents:
            if isinstance(e, MessageEntityUrl):
                urls.add(event.raw_text[e.offset : e.offset + e.length])
            elif isinstance(e, MessageEntityTextUrl) and e.url:
                urls.add(e.url)

        # Bug fix: if entity detection fired but no URLs were extractable (false positive),
        # do NOT return early — fall through so mention checking (Rule 5) still runs.
        for url in urls:
            dom = domain_from_url(url)
            if not dom:
                await block_and_warn(event, "🚫 A malformed link was detected and removed.")
                return
            allowed = any(dom == wd or dom.endswith("." + wd) for wd in whitelisted_domains)
            if not allowed:
                await block_and_warn(event, "🚫 Links from unapproved domains are not allowed.")
                return

    # Rule 5: Mentions policy
    whitelisted_mentions = get_whitelisted_mentions(chat_id)
    usernames = collect_mentions(event)
    for uname in usernames:
        if uname.lower() in whitelisted_mentions:
            continue
        if await is_dangerous_mention(uname):
            await block_and_warn(event, "🚫 Mentions of groups or channels are not allowed.")
            return

@client.on(events.NewMessage)
async def message_handler(event):
    try:
        await enforce_rules(event)
    except Exception as e:
        logging.exception(f"Error in message_handler: {e}")

@client.on(events.MessageEdited)
async def edited_message_handler(event):
    try:
        await enforce_rules(event)
    except Exception as e:
        logging.exception(f"Error in edited_message_handler: {e}")

# -----------------------------------------------------------------------------
# 🚀 Startup
# -----------------------------------------------------------------------------
async def main():
    global BOT_ID
    if not (API_ID and API_HASH and BOT_TOKEN):
        raise RuntimeError("API_ID, API_HASH, and BOT_TOKEN must be set.")
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    BOT_ID = me.id
    logging.info(f"Bot '{me.first_name}' (@{getattr(me, 'username', None)}) is running...")
    asyncio.create_task(periodic_usage_cleanup())
    await client.run_until_disconnected()

if __name__ == "__main__":
    setup_database()
    asyncio.run(main())