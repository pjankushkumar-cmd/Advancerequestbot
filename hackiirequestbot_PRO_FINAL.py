import os
import sys
import json
import base64
import logging
import sqlite3
import threading
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, ContextTypes, MessageHandler, filters
)

# ============================================================
# PRO CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
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
    level=logging.INFO
)

DB = "janeman_pro.db"
LOCK = threading.RLock()

DEFAULTS = {
    "auto_accept": "OFF",
    "request_button_text": "Yes",
    "request_button_action": "START",
    "start_final_id": "",
    "approval_button": "Click Yes",
    "api_enabled": "OFF",
    "api_url": "",
    "api_json_path": "",
    "api_template": "Latest: {value}",
    "api_after_count": "0",
}

CACHED_MESSAGES = []
START_MESSAGES = []
APPROVAL_MESSAGE = None

# ============================================================
# RENDER HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Bot is running."
        self.send_response(200)
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
# SQLITE CACHE
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
        for k in ("total_requests", "accepted"):
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
    with LOCK:
        con = db()
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, str(value))
        )
        con.commit()
        con.close()
    if sync:
        sync_state_to_github()


def stat(key):
    con = db()
    row = con.execute("SELECT count FROM stats WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else 0


def inc_stat(key):
    con = db()
    con.execute("UPDATE stats SET count=count+1 WHERE key=?", (key,))
    con.commit()
    con.close()


def add_user(uid):
    con = db()
    con.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    con.commit()
    con.close()
    sync_members_to_github()


def users():
    con = db()
    out = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
    con.close()
    return out


# ============================================================
# GITHUB PERSISTENCE
# ============================================================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Telegram-Pro-Bot"
    }


def gh_url(path):
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"


def github_enabled():
    return all((GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO))


def gh_get(path):
    if not github_enabled():
        return None
    try:
        r = requests.get(gh_url(path), headers=gh_headers(), timeout=20)
        return r
    except Exception:
        logging.exception("GitHub GET failed")
        return None


def gh_put(path, obj, message):
    if not github_enabled():
        return False
    raw = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    encoded = base64.b64encode(raw.encode()).decode()
    for _ in range(2):
        r = gh_get(path)
        sha = None
        if r is not None and r.status_code == 200:
            sha = r.json().get("sha")
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
        settings = dict(con.execute("SELECT key,value FROM settings").fetchall())
        req = con.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id"
        ).fetchall()
        start = con.execute(
            "SELECT chat_id,msg_id FROM start_messages_list ORDER BY id"
        ).fetchall()
        approval = con.execute(
            "SELECT chat_id,msg_id FROM approval_message WHERE id=1"
        ).fetchone()
        con.close()
    return {
        "version": 3,
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
            con.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                (str(k), str(v))
            )
        con.execute("DELETE FROM messages_list")
        for a, b in state.get("request_messages", []):
            con.execute(
                "INSERT INTO messages_list(chat_id,msg_id) VALUES(?,?)",
                (str(a), str(b))
            )
        con.execute("DELETE FROM start_messages_list")
        for a, b in state.get("start_messages", []):
            con.execute(
                "INSERT INTO start_messages_list(chat_id,msg_id) VALUES(?,?)",
                (str(a), str(b))
            )
        con.execute("DELETE FROM approval_message")
        ap = state.get("approval_message")
        if ap and len(ap) == 2:
            con.execute(
                "INSERT INTO approval_message(id,chat_id,msg_id) VALUES(1,?,?)",
                (str(ap[0]), str(ap[1]))
            )
        con.commit()
        CACHED_MESSAGES = con.execute(
            "SELECT chat_id,msg_id FROM messages_list ORDER BY id"
        ).fetchall()
        START_MESSAGES = con.execute(
            "SELECT chat_id,msg_id FROM start_messages_list ORDER BY id"
        ).fetchall()
        APPROVAL_MESSAGE = con.execute(
            "SELECT chat_id,msg_id FROM approval_message WHERE id=1"
        ).fetchone()
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
        return gh_put(
            GITHUB_STATE_FILE,
            build_state(),
            "Update persistent bot settings"
        )
    except Exception:
        logging.exception("State sync failed")
        return False


# ============================================================
# MESSAGE STORAGE
# ============================================================
def add_message(table, chat_id, msg_id):
    global CACHED_MESSAGES, START_MESSAGES
    con = db()
    con.execute(
        f"INSERT INTO {table}(chat_id,msg_id) VALUES(?,?)",
        (str(chat_id), str(msg_id))
    )
    con.commit()
    rows = con.execute(
        f"SELECT chat_id,msg_id FROM {table} ORDER BY id"
    ).fetchall()
    con.close()
    if table == "messages_list":
        CACHED_MESSAGES = rows
    else:
        START_MESSAGES = rows
    sync_state_to_github()


def clear_messages(table):
    global CACHED_MESSAGES, START_MESSAGES
    con = db()
    con.execute(f"DELETE FROM {table}")
    con.commit()
    con.close()
    if table == "messages_list":
        CACHED_MESSAGES = []
    else:
        START_MESSAGES = []
        set_setting("start_final_id", "", sync=False)
    sync_state_to_github()


def save_approval(chat_id, msg_id):
    global APPROVAL_MESSAGE
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO approval_message(id,chat_id,msg_id) VALUES(1,?,?)",
        (str(chat_id), str(msg_id))
    )
    con.commit()
    con.close()
    APPROVAL_MESSAGE = (str(chat_id), str(msg_id))
    sync_state_to_github()


def clear_approval():
    global APPROVAL_MESSAGE
    con = db()
    con.execute("DELETE FROM approval_message")
    con.commit()
    con.close()
    APPROVAL_MESSAGE = None
    sync_state_to_github()


# ============================================================
# API ENGINE
# ============================================================
def get_path(data, path):
    if not path.strip():
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            return None
    return cur


def api_fetch():
    url = get_setting("api_url").strip()
    if not url:
        return None, "API URL not set"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Telegram-Pro-Bot", "Accept": "application/json"},
            timeout=12
        )
        r.raise_for_status()
        data = r.json()
        value = get_path(data, get_setting("api_json_path"))
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        value = "" if value is None else str(value)
        template = get_setting("api_template") or "{value}"
        text = template.replace("{value}", value)
        return text, None
    except Exception as e:
        logging.error("API fetch failed: %s", e)
        return None, str(e)


async def send_api_result(bot, chat_id):
    if get_setting("api_enabled") != "ON":
        return
    text, err = await asyncio.to_thread(api_fetch)
    if text is None:
        await bot.send_message(chat_id, f"❌ API error: {err}")
    else:
        await bot.send_message(chat_id, text)


# ============================================================
# TELEGRAM UI
# ============================================================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Requests: {stat('total_requests')}"),
         InlineKeyboardButton(f"👥 Members: {len(users())}")],
        [InlineKeyboardButton("📥 Request Settings", callback_data="request_menu")],
        [InlineKeyboardButton("▶️ Start Settings", callback_data="start_menu")],
        [InlineKeyboardButton("🌐 API Settings", callback_data="api_menu")],
        [InlineKeyboardButton("✏️ Approval Settings", callback_data="approval_menu")],
        [InlineKeyboardButton("🔄 Sync Everything", callback_data="sync_all")],
    ])


def request_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add Request Message ({len(CACHED_MESSAGES)})", callback_data="req_add")],
        [InlineKeyboardButton("🗑 Clear Request Messages", callback_data="req_clear")],
        [InlineKeyboardButton(f"🔘 Button: {get_setting('request_button_text')}", callback_data="req_button")],
        [InlineKeyboardButton(f"⚙️ Action: {get_setting('request_button_action')}", callback_data="req_action")],
        [InlineKeyboardButton(f"🤖 Auto Accept: {get_setting('auto_accept')}", callback_data="req_auto")],
        [InlineKeyboardButton("👁 Test Request", callback_data="req_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def start_menu():
    final = get_setting("start_final_id") or "Not Set"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add Start Message ({len(START_MESSAGES)})", callback_data="start_add")],
        [InlineKeyboardButton("🗑 Clear Start Messages", callback_data="start_clear")],
        [InlineKeyboardButton("🎯 Set Final Number", callback_data="start_final")],
        [InlineKeyboardButton(f"FINAL: #{final}", callback_data="noop")],
        [InlineKeyboardButton("👁 Test Start", callback_data="start_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def api_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 API: {get_setting('api_enabled')}", callback_data="api_toggle")],
        [InlineKeyboardButton("🔗 Set API URL", callback_data="api_url")],
        [InlineKeyboardButton(f"📍 JSON Path: {get_setting('api_json_path') or '(root)'}", callback_data="api_path")],
        [InlineKeyboardButton("📝 Set API Message Template", callback_data="api_template")],
        [InlineKeyboardButton(f"🔢 API After N Start Messages: {get_setting('api_after_count')}", callback_data="api_after")],
        [InlineKeyboardButton("🧪 Test API Now", callback_data="api_test")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def approval_menu():
    status = "Saved" if APPROVAL_MESSAGE else "Default"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📝 Message: {status}", callback_data="ap_msg")],
        [InlineKeyboardButton(f"🔘 Button: {get_setting('approval_button')}", callback_data="ap_btn")],
        [InlineKeyboardButton("🗑 Use Default", callback_data="ap_clear")],
        [InlineKeyboardButton("⬅️ Back", callback_data="home")],
    ])


def yes_keyboard():
    text = get_setting("request_button_text")
    if not text or text.upper() == "OFF":
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data="request_yes")]
    ])


async def send_copy(bot, chat_id, row, keyboard=None):
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(row[0]),
            message_id=int(row[1]),
            reply_markup=keyboard
        )
        return True
    except Exception as e:
        logging.error("copy_message failed: %s", e)
        return False


async def send_request_sequence(bot, chat_id):
    if not CACHED_MESSAGES:
        return
    for i, row in enumerate(CACHED_MESSAGES):
        keyboard = yes_keyboard() if i == len(CACHED_MESSAGES) - 1 else None
        await send_copy(bot, chat_id, row, keyboard)


async def send_start_sequence(bot, chat_id):
    if not START_MESSAGES:
        return 0
    final = get_setting("start_final_id").strip()
    count = 0
    for i, row in enumerate(START_MESSAGES, 1):
        if await send_copy(bot, chat_id, row):
            count += 1
        if final and str(i) == final:
            break
    return count


async def send_approval(bot, chat_id):
    label = get_setting("approval_button") or "Click Yes"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="approval_action")]
    ])
    if APPROVAL_MESSAGE:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=int(APPROVAL_MESSAGE[0]),
                message_id=int(APPROVAL_MESSAGE[1]),
                reply_markup=keyboard
            )
            return
        except Exception:
            logging.exception("Approval copy failed")
    await bot.send_message(
        chat_id,
        "Approval message is not configured.",
        reply_markup=keyboard
    )


# ============================================================
# USER FLOW
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    add_user(uid)
    count = await send_start_sequence(context.bot, uid)

    after = get_setting("api_after_count")
    if get_setting("api_enabled") == "ON" and after.isdigit() and int(after) == 0:
        await send_api_result(context.bot, uid)
    elif get_setting("api_enabled") == "ON" and after.isdigit() and int(after) == count:
        await send_api_result(context.bot, uid)

    await send_approval(context.bot, uid)

    if uid == ADMIN_ID and update.message:
        await update.message.reply_text(
            "👑 PRO BOT ADMIN PANEL 👑",
            reply_markup=main_menu()
        )


async def user_reply_to_admin(update, context):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        return
    try:
        name = update.effective_user.full_name or "Unknown"
        username = update.effective_user.username
        header = (
            "📩 New User Reply\n\n"
            f"👤 Name: {name}\n"
            f"🆔 UID: {uid}\n"
            f"🔗 Username: @{username}" if username else
            f"📩 New User Reply\n\n👤 Name: {name}\n🆔 UID: {uid}\n🔗 Username: Not set"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Reply to User", callback_data=f"reply:{uid}")]
        ])
        await context.bot.send_message(ADMIN_ID, header, reply_markup=keyboard)
        await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )
    except Exception:
        logging.exception("Forward user reply failed")


# ============================================================
# ADMIN CALLBACKS
# ============================================================
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "request_yes":
        if uid != ADMIN_ID:
            await q.message.reply_text("⏳ Opening...")
            count = await send_start_sequence(context.bot, uid)
            after = get_setting("api_after_count")
            if get_setting("api_enabled") == "ON" and after.isdigit() and int(after) == count:
                await send_api_result(context.bot, uid)
            await send_approval(context.bot, uid)
        return

    if q.data == "approval_action":
        return

    if uid != ADMIN_ID:
        return

    data = q.data
    if data == "home":
        await q.edit_message_text("👑 PRO BOT ADMIN PANEL 👑", reply_markup=main_menu())
    elif data == "noop":
        pass
    elif data == "sync_all":
        a = await asyncio.to_thread(sync_members_to_github)
        b = await asyncio.to_thread(sync_state_to_github)
        await q.answer("✅ Synced" if a and b else "⚠️ Partial/failed sync", show_alert=True)
    elif data == "request_menu":
        await q.edit_message_text("📥 Request Settings", reply_markup=request_menu())
    elif data == "start_menu":
        await q.edit_message_text("▶️ Start Settings", reply_markup=start_menu())
    elif data == "api_menu":
        await q.edit_message_text("🌐 API Settings", reply_markup=api_menu())
    elif data == "approval_menu":
        await q.edit_message_text("✏️ Approval Settings", reply_markup=approval_menu())
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
        current = get_setting("request_button_action")
        new = "START" if current != "START" else "NONE"
        set_setting("request_button_action", new)
        await q.edit_message_text(f"✅ Button action: {new}", reply_markup=request_menu())
    elif data == "req_auto":
        new = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", new)
        await q.edit_message_text(f"✅ Auto Accept: {new}", reply_markup=request_menu())
    elif data == "req_test":
        await send_request_sequence(context.bot, ADMIN_ID)
    elif data == "start_add":
        context.user_data["state"] = "start_add"
        await q.edit_message_text("📝 /start ke liye message/media bhejo.")
    elif data == "start_clear":
        clear_messages("start_messages_list")
        await q.edit_message_text("✅ Start messages cleared.", reply_markup=start_menu())
    elif data == "start_final":
        context.user_data["state"] = "start_final"
        await q.edit_message_text(f"🎯 Final number bhejo (1-{len(START_MESSAGES)}).")
    elif data == "start_test":
        await send_start_sequence(context.bot, ADMIN_ID)
        await send_approval(context.bot, ADMIN_ID)
    elif data == "api_toggle":
        new = "OFF" if get_setting("api_enabled") == "ON" else "ON"
        set_setting("api_enabled", new)
        await q.edit_message_text(f"🌐 API: {new}", reply_markup=api_menu())
    elif data == "api_url":
        context.user_data["state"] = "api_url"
        await q.edit_message_text("🔗 Full API URL bhejo.")
    elif data == "api_path":
        context.user_data["state"] = "api_path"
        await q.edit_message_text("📍 JSON path bhejo, example: `data.0.number`.\nRoot ke liye `root`.")
    elif data == "api_template":
        context.user_data["state"] = "api_template"
        await q.edit_message_text(
            "📝 Message template bhejo.\n`{value}` API value ko represent karega.\n"
            "Example: `Period: {value}`"
        )
    elif data == "api_after":
        context.user_data["state"] = "api_after"
        await q.edit_message_text(
            "🔢 Kitne Start messages ke baad API message bhejna hai?\n"
            "`0` = Start ke turant baad."
        )
    elif data == "api_test":
        await send_api_result(context.bot, ADMIN_ID)
    elif data == "ap_msg":
        context.user_data["state"] = "ap_msg"
        await q.edit_message_text("📝 Approval message/media bhejo.")
    elif data == "ap_btn":
        context.user_data["state"] = "ap_btn"
        await q.edit_message_text("🔘 Approval button text bhejo.")
    elif data == "ap_clear":
        clear_approval()
        await q.edit_message_text("✅ Default approval restored.", reply_markup=approval_menu())
    elif data.startswith("reply:"):
        target = int(data.split(":", 1)[1])
        context.user_data["reply_to"] = target
        context.user_data["state"] = "admin_reply"
        await q.message.reply_text(f"✍️ UID {target} ko reply bhejo.")


# ============================================================
# ADMIN MESSAGE INPUT
# ============================================================
async def admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    state = context.user_data.get("state")
    m = update.message

    if state == "admin_reply":
        target = context.user_data.get("reply_to")
        if target:
            try:
                await context.bot.copy_message(
                    chat_id=int(target),
                    from_chat_id=m.chat_id,
                    message_id=m.message_id
                )
                await m.reply_text("✅ Reply sent.")
            except Exception as e:
                await m.reply_text(f"❌ Reply failed: {e}")
        context.user_data.clear()
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
        if not t.isdigit() or not (1 <= int(t) <= len(START_MESSAGES)):
            await m.reply_text(f"❌ 1-{len(START_MESSAGES)} ke beech number bhejo.")
            return
        set_setting("start_final_id", t)
        context.user_data.clear()
        await m.reply_text(f"✅ FINAL #{t} saved.", reply_markup=start_menu())
    elif state == "req_button":
        t = (m.text or "").strip()
        if not t:
            await m.reply_text("❌ Text bhejo.")
            return
        set_setting("request_button_text", t)
        context.user_data.clear()
        await m.reply_text("✅ Request button saved.", reply_markup=request_menu())
    elif state == "api_url":
        set_setting("api_url", (m.text or "").strip())
        context.user_data.clear()
        await m.reply_text("✅ API URL saved.", reply_markup=api_menu())
    elif state == "api_path":
        t = (m.text or "").strip()
        set_setting("api_json_path", "" if t.lower() == "root" else t)
        context.user_data.clear()
        await m.reply_text("✅ JSON path saved.", reply_markup=api_menu())
    elif state == "api_template":
        set_setting("api_template", (m.text or "{value}").strip())
        context.user_data.clear()
        await m.reply_text("✅ API template saved.", reply_markup=api_menu())
    elif state == "api_after":
        t = (m.text or "").strip()
        if not t.isdigit():
            await m.reply_text("❌ Sirf number.")
            return
        set_setting("api_after_count", t)
        context.user_data.clear()
        await m.reply_text("✅ API timing saved.", reply_markup=api_menu())
    elif state == "ap_msg":
        save_approval(m.chat_id, m.message_id)
        context.user_data.clear()
        await m.reply_text("✅ Approval message saved.", reply_markup=approval_menu())
    elif state == "ap_btn":
        set_setting("approval_button", (m.text or "").strip())
        context.user_data.clear()
        await m.reply_text("✅ Approval button saved.", reply_markup=approval_menu())


# ============================================================
# JOIN REQUEST
# ============================================================
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


async def sync_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    a = sync_members_to_github()
    b = sync_state_to_github()
    await update.message.reply_text(
        "✅ Everything synced." if a and b else "⚠️ Sync incomplete; check Render logs."
    )


# ============================================================
# MAIN
# ============================================================
def main():
    init_db()
    load_state_from_github()
    load_members_from_github()
    sync_members_to_github()
    sync_state_to_github()

    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sync_members", sync_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, admin_content)
    )
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, user_reply_to_admin)
    )

    logging.info("PRO BOT ONLINE")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
