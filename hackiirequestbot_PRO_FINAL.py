import os
import sys
import json
import base64
import logging
import sqlite3
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, quote

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, Forbidden, BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# PRO CONFIG - NO API / NO EXTERNAL RESULT SERVICE
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "8767998937"))
except ValueError:
    ADMIN_ID = 0

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE = os.getenv("GITHUB_FILE", "members.json")
GITHUB_STATE_FILE = os.getenv("GITHUB_STATE_FILE", "bot_state.json")

if not BOT_TOKEN or not ADMIN_ID:
    print("ERROR: BOT_TOKEN and ADMIN_ID are required.")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

DB = "janeman_pro.db"
LOCK = threading.RLock()

# Settings are deliberately human-editable from the admin panel.
DEFAULTS = {
    "auto_accept": "OFF",
    "request_button_text": "Yes",
    "request_button_action": "START",
    "request_button_payload": "get",
    "start_final_id": "",
    "approval_button": "Click Yes",
    "approval_enabled": "ON",
    "reply_enabled": "ON",
    "reply_header": "📩 New User Reply",
    "reply_include_profile": "ON",
    "broadcast_enabled": "ON",
    "broadcast_delay": "0.05",
}

CACHED_MESSAGES = []
START_MESSAGES = []
APPROVAL_MESSAGE = None

# ============================================================
# RENDER HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = b"Bot is running."
        elif path == "/health":
            body = b"OK"
        else:
            body = b"Not found"
        status = 200 if path in ("/", "/health") else 404
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run_health_server():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ============================================================
# SQLITE
# ============================================================
def db():
    return sqlite3.connect(DB, timeout=30)


def init_db():
    global CACHED_MESSAGES, START_MESSAGES, APPROVAL_MESSAGE
    with LOCK:
        con = db()
        c = con.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
        c.execute("CREATE TABLE IF NOT EXISTS messages_list (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS start_messages_list (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, msg_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS approval_message (id INTEGER PRIMARY KEY CHECK(id=1), chat_id TEXT, msg_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, count INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS start_flow_users (user_id INTEGER PRIMARY KEY, final_reached INTEGER DEFAULT 0)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        for k in ("total_requests", "accepted", "broadcast_sent", "broadcast_failed", "replies_forwarded"):
            c.execute("INSERT OR IGNORE INTO stats(key,count) VALUES(?,0)", (k,))
        con.commit()

        CACHED_MESSAGES = c.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id"
        ).fetchall()
        START_MESSAGES = c.execute(
            "SELECT chat_id,msg_id FROM start_messages_list ORDER BY id"
        ).fetchall()
        APPROVAL_MESSAGE = c.execute(
            "SELECT chat_id,msg_id FROM approval_message WHERE id=1"
        ).fetchone()
        con.close()


def get_setting(key):
    with LOCK:
        con = db()
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        con.close()
    return row[0] if row else DEFAULTS.get(key, "")


def set_setting(key, value, sync=True):
    if key not in DEFAULTS:
        return
    with LOCK:
        con = db()
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, str(value)),
        )
        con.commit()
        con.close()
    if sync:
        threading.Thread(target=sync_state_to_github, daemon=True).start()


def stat(key):
    con = db()
    row = con.execute("SELECT count FROM stats WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else 0


def inc_stat(key, amount=1):
    con = db()
    con.execute("INSERT OR IGNORE INTO stats(key,count) VALUES(?,0)", (key,))
    con.execute("UPDATE stats SET count=count+? WHERE key=?", (amount, key))
    con.commit()
    con.close()


def add_user(uid):
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return
    con = db()
    con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()
    threading.Thread(target=sync_members_to_github, daemon=True).start()


def users():
    con = db()
    out = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    con.close()
    return out


def remove_user(uid):
    con = db()
    con.execute("DELETE FROM users WHERE user_id=?", (int(uid),))
    con.commit()
    con.close()


# ============================================================
# GITHUB PERSISTENCE
# ============================================================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Telegram-Pro-Bot",
    }


def gh_url(path):
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"


def github_enabled():
    return all((GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO))


def gh_get(path):
    if not github_enabled():
        return None
    try:
        return requests.get(gh_url(path), headers=gh_headers(), timeout=20)
    except Exception:
        logging.exception("GitHub GET failed")
        return None


def gh_put(path, obj, message):
    if not github_enabled():
        return False
    raw = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    encoded = base64.b64encode(raw.encode()).decode()
    for _ in range(3):
        r = gh_get(path)
        sha = None
        if r is not None and r.status_code == 200:
            try:
                sha = r.json().get("sha")
            except Exception:
                sha = None
        elif r is not None and r.status_code not in (404,):
            logging.error("GitHub GET %s failed: %s", path, r.text[:800])
            return False

        payload = {"message": message, "content": encoded}
        if sha:
            payload["sha"] = sha
        try:
            p = requests.put(
                gh_url(path), headers=gh_headers(), json=payload, timeout=25
            )
        except Exception:
            logging.exception("GitHub PUT failed")
            return False
        if p.status_code in (200, 201):
            return True
        if p.status_code == 409:
            continue
        logging.error("GitHub PUT %s failed [%s]: %s", path, p.status_code, p.text[:1000])
        return False
    return False


def sync_members_to_github():
    if not github_enabled():
        return False
    remote = []
    r = gh_get(GITHUB_FILE)
    if r is not None and r.status_code == 200:
        try:
            raw = base64.b64decode(r.json().get("content", "")).decode()
            remote = json.loads(raw) if raw.strip() else []
        except Exception:
            remote = []
    elif r is not None and r.status_code != 404:
        logging.error("members read failed: %s", r.text[:800])
        return False
    merged = sorted({int(x) for x in remote} | {int(x) for x in users()})
    return gh_put(GITHUB_FILE, merged, f"Update members.json ({len(merged)} members)")


def load_members_from_github():
    r = gh_get(GITHUB_FILE)
    if not r or r.status_code != 200:
        return
    try:
        raw = base64.b64decode(r.json().get("content", "")).decode()
        arr = json.loads(raw)
        con = db()
        for uid in arr:
            con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(uid),))
        con.commit()
        con.close()
    except Exception:
        logging.exception("Unable to load members.json")


def build_state():
    with LOCK:
        con = db()
        # Explicitly persist only current non-API settings.
        settings = {
            k: v for k, v in con.execute("SELECT key,value FROM settings").fetchall()
            if k in DEFAULTS
        }
        req = con.execute("SELECT chat_id,msg_id FROM messages_list ORDER BY id").fetchall()
        start = con.execute("SELECT chat_id,msg_id FROM start_messages_list ORDER BY id").fetchall()
        approval = con.execute("SELECT chat_id,msg_id FROM approval_message WHERE id=1").fetchone()
        con.close()
    return {
        "version": 7,
        "settings": settings,
        "request_messages": [[str(a), str(b)] for a, b in req],
        "start_messages": [[str(a), str(b)] for a, b in start],
        "approval_message": list(approval) if approval else None,
    }


def restore_state(state):
    global CACHED_MESSAGES, START_MESSAGES, APPROVAL_MESSAGE
    if not isinstance(state, dict):
        return
    with LOCK:
        con = db()
        for k, v in state.get("settings", {}).items():
            if k in DEFAULTS:
                con.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (str(k), str(v)),
                )
        con.execute("DELETE FROM messages_list")
        for item in state.get("request_messages", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                con.execute(
                    "INSERT INTO messages_list(chat_id,msg_id) VALUES(?,?)",
                    (str(item[0]), str(item[1])),
                )
        con.execute("DELETE FROM start_messages_list")
        for item in state.get("start_messages", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                con.execute(
                    "INSERT INTO start_messages_list(chat_id,msg_id) VALUES(?,?)",
                    (str(item[0]), str(item[1])),
                )
        con.execute("DELETE FROM approval_message")
        ap = state.get("approval_message")
        if isinstance(ap, (list, tuple)) and len(ap) == 2:
            con.execute(
                "INSERT INTO approval_message(id,chat_id,msg_id) VALUES(1,?,?)",
                (str(ap[0]), str(ap[1])),
            )
        con.commit()
        CACHED_MESSAGES = con.execute("SELECT chat_id,msg_id FROM messages_list ORDER BY id").fetchall()
        START_MESSAGES = con.execute("SELECT chat_id,msg_id FROM start_messages_list ORDER BY id").fetchall()
        APPROVAL_MESSAGE = con.execute("SELECT chat_id,msg_id FROM approval_message WHERE id=1").fetchone()
        con.close()


def load_state_from_github():
    r = gh_get(GITHUB_STATE_FILE)
    if not r or r.status_code != 200:
        return False
    try:
        raw = base64.b64decode(r.json().get("content", "")).decode()
        restore_state(json.loads(raw))
        logging.info("Persistent bot state restored from GitHub.")
        return True
    except Exception:
        logging.exception("State restore failed")
        return False


def sync_state_to_github():
    try:
        return gh_put(GITHUB_STATE_FILE, build_state(), "Update persistent bot settings")
    except Exception:
        logging.exception("State sync failed")
        return False


# ============================================================
# MESSAGE STORAGE
# ============================================================
def add_message(table, chat_id, msg_id):
    global CACHED_MESSAGES, START_MESSAGES
    if table not in ("messages_list", "start_messages_list"):
        return
    con = db()
    con.execute(
        f"INSERT INTO {table}(chat_id,msg_id) VALUES(?,?)",
        (str(chat_id), str(msg_id)),
    )
    con.commit()
    rows = con.execute(f"SELECT chat_id,msg_id FROM {table} ORDER BY id").fetchall()
    con.close()
    if table == "messages_list":
        CACHED_MESSAGES = rows
    else:
        START_MESSAGES = rows
    threading.Thread(target=sync_state_to_github, daemon=True).start()


def clear_messages(table):
    global CACHED_MESSAGES, START_MESSAGES
    if table not in ("messages_list", "start_messages_list"):
        return
    con = db()
    con.execute(f"DELETE FROM {table}")
    con.commit()
    con.close()
    if table == "messages_list":
        CACHED_MESSAGES = []
    else:
        START_MESSAGES = []
        set_setting("start_final_id", "", sync=False)
    threading.Thread(target=sync_state_to_github, daemon=True).start()


def save_approval(chat_id, msg_id):
    global APPROVAL_MESSAGE
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO approval_message(id,chat_id,msg_id) VALUES(1,?,?)",
        (str(chat_id), str(msg_id)),
    )
    con.commit()
    con.close()
    APPROVAL_MESSAGE = (str(chat_id), str(msg_id))
    threading.Thread(target=sync_state_to_github, daemon=True).start()


def clear_approval():
    global APPROVAL_MESSAGE
    con = db()
    con.execute("DELETE FROM approval_message")
    con.commit()
    con.close()
    APPROVAL_MESSAGE = None
    threading.Thread(target=sync_state_to_github, daemon=True).start()


# ============================================================
# TELEGRAM UI
# ============================================================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Requests: {stat('total_requests')}", callback_data="stats"),
         InlineKeyboardButton(f"👥 Members: {len(users())}", callback_data="stats")],
        [InlineKeyboardButton("📥 Request Settings", callback_data="request_menu")],
        [InlineKeyboardButton("▶️ Start Settings", callback_data="start_menu")],
        [InlineKeyboardButton("✏️ Approval Settings", callback_data="approval_menu")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast_menu")],
        [InlineKeyboardButton("💬 Reply Settings", callback_data="reply_menu")],
        [InlineKeyboardButton("🔄 Sync Everything", callback_data="sync_all")],
    ])


def request_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add Request Message ({len(CACHED_MESSAGES)})", callback_data="req_add")],
        [InlineKeyboardButton("🗑 Clear Request Messages", callback_data="req_clear")],
        [InlineKeyboardButton(f"🔘 Button: {get_setting('request_button_text')}", callback_data="req_button")],
        [InlineKeyboardButton(f"⚙️ Action: {get_setting('request_button_action')}", callback_data="req_action")],
        [InlineKeyboardButton(f"🔗 /start Payload: {get_setting('request_button_payload') or 'OFF'}", callback_data="req_payload")],
        [InlineKeyboardButton(f"🤖 Auto Accept: {get_setting('auto_accept')}", callback_data="req_auto")],
        [InlineKeyboardButton("👁 Test Request", callback_data="req_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def start_menu():
    final = get_setting("start_final_id") or "All"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add Start Message ({len(START_MESSAGES)})", callback_data="start_add")],
        [InlineKeyboardButton("🗑 Clear Start Messages", callback_data="start_clear")],
        [InlineKeyboardButton(f"🎯 Final Message: {final}", callback_data="start_final")],
        [InlineKeyboardButton("👁 Test Start", callback_data="start_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def approval_menu():
    status = "Saved" if APPROVAL_MESSAGE else "Not Set"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 Message: {status}", callback_data="ap_msg")],
        [InlineKeyboardButton(f"🔘 Button: {get_setting('approval_button')}", callback_data="ap_btn")],
        [InlineKeyboardButton(f"📌 Enabled: {get_setting('approval_enabled')}", callback_data="ap_toggle")],
        [InlineKeyboardButton("🗑 Clear Message", callback_data="ap_clear")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def reply_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💬 Forward Replies: {get_setting('reply_enabled')}", callback_data="reply_toggle")],
        [InlineKeyboardButton(f"👤 Profile Info: {get_setting('reply_include_profile')}", callback_data="reply_profile")],
        [InlineKeyboardButton("📝 Set Reply Header", callback_data="reply_header")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def broadcast_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📢 Broadcast: {get_setting('broadcast_enabled')}", callback_data="bc_toggle")],
        [InlineKeyboardButton(f"⚡ Delay: {get_setting('broadcast_delay')}s", callback_data="bc_delay")],
        [InlineKeyboardButton("🚀 Send Broadcast", callback_data="bc_start")],
        [InlineKeyboardButton("📊 Broadcast Stats", callback_data="bc_stats")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


async def yes_keyboard(bot):
    text = get_setting("request_button_text").strip()
    if not text or text.upper() == "OFF":
        return None
    payload = get_setting("request_button_payload").strip()
    if payload and payload.upper() != "OFF":
        try:
            me = await bot.get_me()
            if me.username:
                url = f"https://t.me/{me.username}?start={quote(payload, safe='') }"
                return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])
        except Exception:
            logging.exception("Could not build /start deep-link")
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data="request_yes")]])


async def send_copy(bot, chat_id, row, keyboard=None):
    for attempt in range(3):
        try:
            await bot.copy_message(
                chat_id=int(chat_id),
                from_chat_id=int(row[0]),
                message_id=int(row[1]),
                reply_markup=keyboard,
            )
            return True
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except (Forbidden, BadRequest) as e:
            logging.warning("copy_message to %s failed: %s", chat_id, e)
            return False
        except TelegramError as e:
            logging.warning("copy_message to %s failed: %s", chat_id, e)
            if attempt < 2:
                await asyncio.sleep(1.0)
        except Exception:
            logging.exception("copy_message failed")
            return False
    return False


async def send_request_sequence(bot, chat_id):
    if not CACHED_MESSAGES:
        return 0
    sent = 0
    keyboard = await yes_keyboard(bot)
    for i, row in enumerate(CACHED_MESSAGES):
        kb = keyboard if i == len(CACHED_MESSAGES) - 1 else None
        if await send_copy(bot, chat_id, row, kb):
            sent += 1
    return sent


async def send_start_sequence(bot, chat_id):
    if not START_MESSAGES:
        return 0
    final_text = get_setting("start_final_id").strip()
    final = int(final_text) if final_text.isdigit() else len(START_MESSAGES)
    final = max(1, min(final, len(START_MESSAGES)))
    sent = 0
    for i, row in enumerate(START_MESSAGES, 1):
        if await send_copy(bot, chat_id, row):
            sent += 1
        if i >= final:
            break
    return sent


async def send_approval(bot, chat_id):
    if get_setting("approval_enabled") != "ON" or not APPROVAL_MESSAGE:
        return False
    label = get_setting("approval_button").strip()
    keyboard = None
    if label and label.upper() != "OFF":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="approval_action")]])
    return await send_copy(bot, chat_id, APPROVAL_MESSAGE, keyboard)


# ============================================================
# START FLOW / REPLIES
# ============================================================
def set_flow_state(uid, final_reached):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO start_flow_users(user_id,final_reached) VALUES(?,?)",
        (int(uid), 1 if final_reached else 0),
    )
    con.commit()
    con.close()


def flow_finished(uid):
    con = db()
    row = con.execute(
        "SELECT final_reached FROM start_flow_users WHERE user_id=?",
        (int(uid),),
    ).fetchone()
    con.close()
    return bool(row and row[0] == 1)


async def run_start_flow(bot, uid, mark_user=True):
    uid = int(uid)
    set_flow_state(uid, False)

    if not START_MESSAGES:
        if mark_user:
            await bot.send_message(uid, "⚠️ Start messages abhi configure nahi hain.")
        return 0

    final_text = get_setting("start_final_id").strip()
    final = int(final_text) if final_text.isdigit() else len(START_MESSAGES)
    final = max(1, min(final, len(START_MESSAGES)))

    sent_count = 0
    for i, row in enumerate(START_MESSAGES, 1):
        if await send_copy(bot, uid, row):
            sent_count += 1
        if i >= final:
            break

    # As soon as the configured final message is completed, replies are accepted.
    if mark_user:
        set_flow_state(uid, True)

    await send_approval(bot, uid)
    return sent_count


# ============================================================
# FAST BROADCAST
# ============================================================
BROADCAST_SEMAPHORE = asyncio.Semaphore(18)


async def broadcast_copy(bot, target, source_chat_id, source_message_id):
    async with BROADCAST_SEMAPHORE:
        for attempt in range(4):
            try:
                await bot.copy_message(
                    chat_id=int(target),
                    from_chat_id=int(source_chat_id),
                    message_id=int(source_message_id),
                )
                return True
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 0.5)
            except Forbidden:
                return False
            except BadRequest as e:
                logging.warning("Broadcast BadRequest to %s: %s", target, e)
                return False
            except TelegramError as e:
                logging.warning("Broadcast TelegramError to %s: %s", target, e)
                if attempt < 3:
                    await asyncio.sleep(0.8 * (attempt + 1))
            except Exception:
                logging.exception("Broadcast failed for %s", target)
                return False
    return False


async def run_broadcast(bot, source_chat_id, source_message_id):
    if get_setting("broadcast_enabled") != "ON":
        return 0, 0

    member_ids = users()
    delay_text = get_setting("broadcast_delay").strip()
    try:
        delay = max(0.0, min(float(delay_text), 2.0))
    except ValueError:
        delay = 0.05

    sent = 0
    failed = 0

    async def one(uid):
        nonlocal sent, failed
        ok = await broadcast_copy(bot, uid, source_chat_id, source_message_id)
        if ok:
            sent += 1
            inc_stat("broadcast_sent")
        else:
            failed += 1
            inc_stat("broadcast_failed")
        if delay:
            await asyncio.sleep(delay)

    # Bounded concurrency keeps it fast while respecting Telegram flood limits.
    await asyncio.gather(*(one(uid) for uid in member_ids))
    return sent, failed


# ============================================================
# COMMANDS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        context.user_data.clear()
        await update.message.reply_text("👑 PRO BOT ADMIN PANEL 👑", reply_markup=main_menu())
        return

    add_user(uid)
    await run_start_flow(context.bot, uid, mark_user=True)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.clear()
    await update.message.reply_text("👑 PRO BOT ADMIN PANEL 👑", reply_markup=main_menu())


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    a = await asyncio.to_thread(sync_members_to_github)
    b = await asyncio.to_thread(sync_state_to_github)
    await update.message.reply_text(
        "✅ Everything synced." if a and b else "⚠️ Sync incomplete; check Render logs."
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.clear()
    context.user_data["state"] = "broadcast_content"
    await update.message.reply_text(
        f"📢 Broadcast mode ON\n👥 Members: {len(users())}\n\n"
        "Ab jo message/media bhejoge, woh sab members ko copy hoga.\n"
        "Cancel ke liye /admin bhejo."
    )


# ============================================================
# USER -> ADMIN REPLIES
# ============================================================
async def user_reply_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    m = update.message
    if uid == ADMIN_ID or not m:
        return
    if get_setting("reply_enabled") != "ON":
        return
    if not flow_finished(uid):
        return

    try:
        name = update.effective_user.full_name or "Unknown"
        username = update.effective_user.username
        username_text = f"@{username}" if username else "Not set"
        header = get_setting("reply_header") or "📩 New User Reply"
        if get_setting("reply_include_profile") == "ON":
            header = (
                f"{header}\n\n"
                f"👤 Name: {name}\n"
                f"🆔 UID: {uid}\n"
                f"🔗 Username: {username_text}"
            )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Reply to User", callback_data=f"reply:{uid}")]
        ])

        await context.bot.send_message(ADMIN_ID, header, reply_markup=keyboard)
        await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=m.chat_id,
            message_id=m.message_id,
        )
        inc_stat("replies_forwarded")
    except Exception:
        logging.exception("Forward user reply failed")


# ============================================================
# ADMIN CALLBACKS
# ============================================================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data == "request_yes":
        if uid != ADMIN_ID:
            if get_setting("request_button_action") == "START":
                add_user(uid)
                await run_start_flow(context.bot, uid, mark_user=True)
        return

    if data == "approval_action":
        return

    if uid != ADMIN_ID:
        return

    if data == "home":
        await q.edit_message_text("👑 PRO BOT ADMIN PANEL 👑", reply_markup=main_menu())

    elif data == "stats":
        await q.answer(
            f"Requests: {stat('total_requests')} | Members: {len(users())} | Replies: {stat('replies_forwarded')}",
            show_alert=True,
        )

    elif data == "sync_all":
        a = await asyncio.to_thread(sync_members_to_github)
        b = await asyncio.to_thread(sync_state_to_github)
        await q.answer("✅ Synced" if a and b else "⚠️ Partial/failed sync", show_alert=True)

    elif data == "request_menu":
        await q.edit_message_text("📥 Request Settings", reply_markup=request_menu())
    elif data == "start_menu":
        await q.edit_message_text("▶️ Start Settings", reply_markup=start_menu())
    elif data == "approval_menu":
        await q.edit_message_text("✏️ Approval Settings", reply_markup=approval_menu())
    elif data == "reply_menu":
        await q.edit_message_text("💬 Reply Settings", reply_markup=reply_menu())
    elif data == "broadcast_menu":
        await q.edit_message_text("📢 Broadcast Settings", reply_markup=broadcast_menu())
    elif data == "noop":
        return

    # Request settings
    elif data == "req_add":
        context.user_data["state"] = "req_add"
        await q.edit_message_text("📝 Request session ke liye message/media bhejo.")
    elif data == "req_clear":
        clear_messages("messages_list")
        await q.edit_message_text("✅ Request messages cleared.", reply_markup=request_menu())
    elif data == "req_button":
        context.user_data["state"] = "req_button"
        await q.edit_message_text("🔘 Button text bhejo. `OFF` se button hide hoga.")
    elif data == "req_action":
        new = "NONE" if get_setting("request_button_action") == "START" else "START"
        set_setting("request_button_action", new)
        await q.edit_message_text(f"✅ Button action: {new}", reply_markup=request_menu())
    elif data == "req_payload":
        context.user_data["state"] = "req_payload"
        await q.edit_message_text("🔗 Hidden /start payload bhejo. Example: get\n`OFF` = normal callback.")
    elif data == "req_auto":
        new = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", new)
        await q.edit_message_text(f"✅ Auto Accept: {new}", reply_markup=request_menu())
    elif data == "req_test":
        await send_request_sequence(context.bot, ADMIN_ID)

    # Start settings
    elif data == "start_add":
        context.user_data["state"] = "start_add"
        await q.edit_message_text("📝 /start ke liye message/media bhejo.")
    elif data == "start_clear":
        clear_messages("start_messages_list")
        await q.edit_message_text("✅ Start messages cleared.", reply_markup=start_menu())
    elif data == "start_final":
        context.user_data["state"] = "start_final"
        await q.edit_message_text(f"🎯 Final message number bhejo (1-{len(START_MESSAGES)}).\n`0` = all messages.")
    elif data == "start_test":
        await run_start_flow(context.bot, ADMIN_ID, mark_user=False)

    # Approval
    elif data == "ap_msg":
        context.user_data["state"] = "ap_msg"
        await q.edit_message_text("📝 Approval message/media bhejo.")
    elif data == "ap_btn":
        context.user_data["state"] = "ap_btn"
        await q.edit_message_text("🔘 Approval button text bhejo. `OFF` se button hide hoga.")
    elif data == "ap_toggle":
        new = "OFF" if get_setting("approval_enabled") == "ON" else "ON"
        set_setting("approval_enabled", new)
        await q.edit_message_text(f"📌 Approval: {new}", reply_markup=approval_menu())
    elif data == "ap_clear":
        clear_approval()
        await q.edit_message_text("✅ Approval message cleared.", reply_markup=approval_menu())

    # Replies
    elif data == "reply_toggle":
        new = "OFF" if get_setting("reply_enabled") == "ON" else "ON"
        set_setting("reply_enabled", new)
        await q.edit_message_text(f"💬 Forward Replies: {new}", reply_markup=reply_menu())
    elif data == "reply_profile":
        new = "OFF" if get_setting("reply_include_profile") == "ON" else "ON"
        set_setting("reply_include_profile", new)
        await q.edit_message_text(f"👤 Profile Info: {new}", reply_markup=reply_menu())
    elif data == "reply_header":
        context.user_data["state"] = "reply_header"
        await q.edit_message_text("📝 Reply header bhejo.")

    # Broadcast
    elif data == "bc_toggle":
        new = "OFF" if get_setting("broadcast_enabled") == "ON" else "ON"
        set_setting("broadcast_enabled", new)
        await q.edit_message_text(f"📢 Broadcast: {new}", reply_markup=broadcast_menu())
    elif data == "bc_delay":
        context.user_data["state"] = "bc_delay"
        await q.edit_message_text("⚡ Delay seconds bhejo (0 to 2). Fast ke liye 0.05 recommended.")
    elif data == "bc_start":
        context.user_data["state"] = "broadcast_content"
        await q.edit_message_text(
            f"🚀 Broadcast ready\n👥 {len(users())} members\n\n"
            "Ab ek text/message/media bhejo. Cancel: /admin"
        )
    elif data == "bc_stats":
        await q.answer(
            f"Sent: {stat('broadcast_sent')} | Failed: {stat('broadcast_failed')}",
            show_alert=True,
        )

    # Reply to specific user
    elif data.startswith("reply:"):
        try:
            target = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("Invalid UID", show_alert=True)
            return
        context.user_data["reply_to"] = target
        context.user_data["state"] = "admin_reply"
        await q.message.reply_text(f"✍️ UID {target} ko reply bhejo.")


# ============================================================
# ADMIN MESSAGE INPUT
# ============================================================
async def admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not update.message:
        return

    state = context.user_data.get("state")
    m = update.message

    if state == "admin_reply":
        target = context.user_data.get("reply_to")
        if target:
            try:
                await context.bot.copy_message(
                    chat_id=int(target), from_chat_id=m.chat_id, message_id=m.message_id
                )
                await m.reply_text("✅ Reply sent.")
            except Exception as e:
                await m.reply_text(f"❌ Reply failed: {e}")
        context.user_data.clear()
        return

    if state == "broadcast_content":
        if get_setting("broadcast_enabled") != "ON":
            context.user_data.clear()
            await m.reply_text("⚠️ Broadcast OFF hai.", reply_markup=main_menu())
            return
        context.user_data.clear()
        progress = await m.reply_text(f"📢 Broadcast starting...\n👥 {len(users())} members")
        sent, failed = await run_broadcast(context.bot, m.chat_id, m.message_id)
        try:
            await progress.edit_text(
                f"✅ Broadcast complete\n\n👥 Total: {sent + failed}\n✅ Sent: {sent}\n❌ Failed: {failed}"
            )
        except Exception:
            await m.reply_text(f"✅ Sent: {sent}\n❌ Failed: {failed}")
        return

    if state == "req_add":
        add_message("messages_list", m.chat_id, m.message_id)
        context.user_data.clear()
        await m.reply_text(f"✅ Request message #{len(CACHED_MESSAGES)} saved.", reply_markup=request_menu())
    elif state == "start_add":
        add_message("start_messages_list", m.chat_id, m.message_id)
        context.user_data.clear()
        await m.reply_text(f"✅ Start message #{len(START_MESSAGES)} saved.", reply_markup=start_menu())
    elif state == "start_final":
        t = (m.text or "").strip()
        if not t.isdigit() or not (0 <= int(t) <= len(START_MESSAGES)):
            await m.reply_text(f"❌ 0-{len(START_MESSAGES)} ke beech number bhejo.")
            return
        set_setting("start_final_id", "" if t == "0" else t)
        context.user_data.clear()
        await m.reply_text("✅ Final message saved.", reply_markup=start_menu())
    elif state == "req_button":
        t = (m.text or "").strip()
        if not t:
            await m.reply_text("❌ Text bhejo.")
            return
        set_setting("request_button_text", t)
        context.user_data.clear()
        await m.reply_text("✅ Request button saved.", reply_markup=request_menu())
    elif state == "req_payload":
        t = (m.text or "").strip() or "OFF"
        set_setting("request_button_payload", t)
        context.user_data.clear()
        await m.reply_text("✅ Hidden /start payload saved.", reply_markup=request_menu())
    elif state == "ap_msg":
        save_approval(m.chat_id, m.message_id)
        context.user_data.clear()
        await m.reply_text("✅ Approval message saved.", reply_markup=approval_menu())
    elif state == "ap_btn":
        set_setting("approval_button", (m.text or "").strip() or "OFF")
        context.user_data.clear()
        await m.reply_text("✅ Approval button saved.", reply_markup=approval_menu())
    elif state == "reply_header":
        set_setting("reply_header", (m.text or "").strip() or "📩 New User Reply")
        context.user_data.clear()
        await m.reply_text("✅ Reply header saved.", reply_markup=reply_menu())
    elif state == "bc_delay":
        t = (m.text or "").strip()
        try:
            value = float(t)
            if not 0 <= value <= 2:
                raise ValueError
        except ValueError:
            await m.reply_text("❌ 0 se 2 seconds ke beech value bhejo.")
            return
        set_setting("broadcast_delay", str(value))
        context.user_data.clear()
        await m.reply_text("✅ Broadcast delay saved.", reply_markup=broadcast_menu())


# ============================================================
# MESSAGE ROUTER / JOIN REQUEST
# ============================================================
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if update.effective_user.id == ADMIN_ID:
        await admin_content(update, context)
    else:
        await user_reply_to_admin(update, context)


async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return
    inc_stat("total_requests")
    add_user(req.from_user.id)
    await send_request_sequence(context.bot, req.from_user.id)
    if get_setting("auto_accept") == "ON":
        try:
            await context.bot.approve_chat_join_request(req.chat.id, req.from_user.id)
            inc_stat("accepted")
        except Exception:
            logging.exception("Auto accept failed")


# ============================================================
# MAIN
# ============================================================
def main():
    init_db()
    load_state_from_github()
    load_members_from_github()
    threading.Thread(target=sync_members_to_github, daemon=True).start()
    threading.Thread(target=sync_state_to_github, daemon=True).start()
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("sync_members", sync_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))

    logging.info("PRO BOT ONLINE | API SYSTEM REMOVED | ADMIN_ID=%s", ADMIN_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
