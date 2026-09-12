import os
import sys
import json
import base64
import logging
import re
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
    level=logging.INFO
)

DB = "janeman_pro.db"
LOCK = threading.RLock()

DEFAULTS = {
    "auto_accept": "OFF",
    "request_button_text": "Yes",
    "request_button_action": "START",
    "request_start_payload": "",
    "start_final_id": "",
    "approval_button": "Click Yes",
    "api_enabled": "OFF",
    "api_url": "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json?pageNo=1",
    "api_json_path": "",
    "api_template": "{value}",
    "api_after_count": "0",
}

CACHED_MESSAGES = []
START_MESSAGES = []
APPROVAL_MESSAGE = None

# ============================================================
# RENDER HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def _send(self, body, content_type="text/plain; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        # Same-origin API endpoint for the bundled index.html.
        # This keeps the browser away from the source API and avoids browser
        # CORS/403 problems.
        if path == "/api/latest":
            try:
                issue, err = api_fetch()
                if issue is None:
                    self._send(
                        json.dumps({"ok": False, "error": err}, ensure_ascii=False),
                        "application/json; charset=utf-8",
                        502
                    )
                else:
                    self._send(
                        json.dumps({"ok": True, "issue": issue}, ensure_ascii=False),
                        "application/json; charset=utf-8"
                    )
            except Exception as exc:
                self._send(
                    json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
                    "application/json; charset=utf-8",
                    500
                )
            return

        if path in ("/", "/index.html"):
            try:
                index_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "index.html"
                )
                with open(index_path, "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(b"Bot is running. index.html not found.", status=200)
            return

        self._send(b"Bot is running.")

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
    # Do not make Telegram users wait for GitHub network I/O.
    threading.Thread(
        target=sync_members_to_github,
        daemon=True
    ).start()


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
        "version": 4,
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

WIN_GO_API = (
    "https://draw.ar-lottery01.com/WinGo/WinGo_1M/"
    "GetHistoryIssuePage.json?pageNo=1"
)

API_FALLBACKS = [
    WIN_GO_API,
    "https://api.allorigins.win/raw?url=" + WIN_GO_API,
    "https://corsproxy.io/?" + WIN_GO_API,
    "https://r.jina.ai/" + WIN_GO_API,
]


def get_path(data, path):
    if not path or path.strip().lower() in ("root", "$"):
        return data

    cur = data
    path = path.strip().lstrip("$.")
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            return None
    return cur


def _find_records(data):
    """Find the API's history array without assuming one exact wrapper."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    for key in (
        "data", "list", "records", "rows", "items", "history",
        "result", "results"
    ):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _find_records(value)
            if nested:
                return nested
    return []


def _find_value(obj, keys):
    if not isinstance(obj, dict):
        return None

    for key in keys:
        value = obj.get(key)
        if value is not None and str(value).strip() != "":
            return value

    for value in obj.values():
        if isinstance(value, dict):
            found = _find_value(value, keys)
            if found is not None:
                return found
        elif isinstance(value, list):
            for item in value[:3]:
                if isinstance(item, dict):
                    found = _find_value(item, keys)
                    if found is not None:
                        return found
    return None


def _issue_from_record(record):
    return _find_value(record, (
        "issueNumber", "issue", "period", "periodNumber",
        "issueNo", "issueNum", "drawNumber", "lotteryIssue",
        "issue_number", "periodNumber"
    ))


def _next_issue(issue):
    """
    Return the next issue/period while preserving leading zeroes.
    Example: 202609121234 -> 202609121235.
    """
    if issue is None:
        return None

    s = str(issue).strip()
    if not s or not s.isdigit():
        return None

    width = len(s)
    try:
        return str(int(s) + 1).zfill(width)
    except Exception:
        return None


def _decode_json_response(response):
    text = response.text.strip()
    if not text:
        raise ValueError("Empty API response")

    try:
        return response.json()
    except Exception:
        # Some proxy responses can contain surrounding text.
        first = min(
            [i for i in (text.find("{"), text.find("[")) if i >= 0],
            default=-1
        )
        last = max(text.rfind("}"), text.rfind("]"))
        if first >= 0 and last > first:
            return json.loads(text[first:last + 1])
        raise ValueError("API did not return valid JSON")


def _api_get(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://draw.ar-lottery01.com/",
        "Origin": "https://draw.ar-lottery01.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return _decode_json_response(response)


def api_fetch():
    """
    Fetch latest WinGo issue. If the source returns 403/CORS-like blocking,
    automatically tries the configured URL and safe HTTP fallbacks.

    The bot intentionally DOES NOT expose the result number.
    It returns only the NEXT Issue/Period number.
    """
    configured = get_setting("api_url").strip() or WIN_GO_API

    sources = [configured]
    if configured == WIN_GO_API:
        sources = API_FALLBACKS[:]
    else:
        # Still keep fallbacks for the standard WinGo endpoint.
        sources.extend(
            x for x in API_FALLBACKS[1:]
            if x not in sources
        )

    errors = []

    for url in sources:
        try:
            data = _api_get(url)

            # Manual JSON path takes priority if configured.
            path = get_setting("api_json_path").strip()
            if path:
                value = get_path(data, path)
                if isinstance(value, list):
                    record = value[0] if value else None
                elif isinstance(value, dict):
                    record = value
                else:
                    record = None
            else:
                records = _find_records(data)
                record = records[0] if records else (
                    data if isinstance(data, dict) else None
                )

            if not record or not isinstance(record, dict):
                raise ValueError("Latest issue record not found")

            latest_issue = _issue_from_record(record)
            next_issue = _next_issue(latest_issue)

            if next_issue is None:
                raise ValueError(
                    f"Latest Issue/Period not found in API record: {record}"
                )

            # Only the period/issue is returned.
            return next_issue, None

        except Exception as exc:
            errors.append(f"{url}: {exc}")
            logging.warning("API source failed: %s", exc)

    return None, " | ".join(errors[-3:])


async def send_api_result(bot, chat_id):
    if get_setting("api_enabled") != "ON":
        return

    issue, err = await asyncio.to_thread(api_fetch)

    if issue is None:
        await bot.send_message(
            chat_id,
            f"❌ API error: {err}"
        )
        return

    # ONLY the next Issue Number/Period. No result number.
    await bot.send_message(chat_id, issue)


# ============================================================
# TELEGRAM UI
# ============================================================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Requests: {stat('total_requests')}", callback_data="stats"),
         InlineKeyboardButton(f"👥 Members: {len(users())}", callback_data="stats")],
        [InlineKeyboardButton("📥 Request Settings", callback_data="request_menu")],
        [InlineKeyboardButton("▶️ Start Settings", callback_data="start_menu")],
        [InlineKeyboardButton("🌐 API Settings", callback_data="api_menu")],
        [InlineKeyboardButton("✏️ Approval Settings", callback_data="approval_menu")],
        [InlineKeyboardButton("🔄 Sync Everything", callback_data="sync_all")],
    ])


def request_menu():
    payload = get_setting("request_start_payload") or "Not Set"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ Add Request Message ({len(CACHED_MESSAGES)})", callback_data="req_add")],
        [InlineKeyboardButton("🗑 Clear Request Messages", callback_data="req_clear")],
        [InlineKeyboardButton(f"🔘 Button: {get_setting('request_button_text')}", callback_data="req_button")],
        [InlineKeyboardButton(f"⚙️ Action: {get_setting('request_button_action')}", callback_data="req_action")],
        [InlineKeyboardButton(f"🔗 Start Payload: {payload}", callback_data="req_payload")],
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


async def yes_keyboard(bot):
    text = get_setting("request_button_text")
    if not text or text.upper() == "OFF":
        return None

    action = get_setting("request_button_action").strip().upper()
    if action == "START":
        payload = get_setting("request_start_payload").strip()
        try:
            me = await bot.get_me()
            username = me.username
        except Exception:
            username = None

        # Telegram deep-link payload is optional. It remains hidden behind
        # the button; the user sees only the configured button text.
        if username:
            link = f"https://t.me/{username}?start"
            if payload:
                # Telegram start payload accepts letters, digits, _ and -.
                safe = re.sub(r"[^A-Za-z0-9_-]", "_", payload)[:64]
                if safe:
                    link += f"={safe}"
            return InlineKeyboardMarkup([
                [InlineKeyboardButton(text, url=link)]
            ])

    # Backward-compatible callback button if START deep-link cannot be made.
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
    button_keyboard = await yes_keyboard(bot)
    for i, row in enumerate(CACHED_MESSAGES):
        keyboard = button_keyboard if i == len(CACHED_MESSAGES) - 1 else None
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
    # Approval message is optional. If it is not configured, do nothing.
    if not APPROVAL_MESSAGE:
        return False

    label = get_setting("approval_button") or "Click Yes"
    keyboard = None
    if label and label.upper() != "OFF":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data="approval_action")]
        ])

    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(APPROVAL_MESSAGE[0]),
            message_id=int(APPROVAL_MESSAGE[1]),
            reply_markup=keyboard
        )
        return True
    except Exception:
        logging.exception("Approval copy failed")
        return False


# ============================================================
# USER FLOW
# ============================================================
def set_flow_state(uid, final_reached):
    con = db()
    con.execute(
        "INSERT OR REPLACE INTO start_flow_users(user_id,final_reached) VALUES(?,?)",
        (int(uid), 1 if final_reached else 0)
    )
    con.commit()
    con.close()


def flow_finished(uid):
    con = db()
    row = con.execute(
        "SELECT final_reached FROM start_flow_users WHERE user_id=?",
        (int(uid),)
    ).fetchone()
    con.close()
    return bool(row and row[0] == 1)


def api_after_number():
    value = get_setting("api_after_count")
    return int(value) if value.isdigit() else 0


async def run_start_flow(bot, uid, mark_user=True, start_payload=""):
    """
    Sends Start messages up to FINAL.

    start_payload is the hidden Telegram /start payload from a button
    deep-link. It is accepted without changing the existing Start sequence.
    
    API is sent exactly after N successful Start messages when configured.
    If N=0, API is sent after the Start sequence.
    FINAL marks the point after which user replies are forwarded to admin.
    """
    uid = int(uid)
    set_flow_state(uid, False)

    if not START_MESSAGES:
        logging.warning("Start flow requested but no start messages are configured.")
        if mark_user:
            await bot.send_message(
                uid,
                "⚠️ Start messages abhi configure nahi hain."
            )
        return 0

    final_text = get_setting("start_final_id").strip()
    final = int(final_text) if final_text.isdigit() else len(START_MESSAGES)
    final = max(1, min(final, len(START_MESSAGES)))

    api_enabled = get_setting("api_enabled") == "ON"
    api_after = api_after_number()
    api_sent = False
    sent_count = 0

    for i, row in enumerate(START_MESSAGES, 1):
        ok = await send_copy(bot, uid, row)
        if ok:
            sent_count += 1

        if api_enabled and api_after > 0 and sent_count == api_after and not api_sent:
            await send_api_result(bot, uid)
            api_sent = True

        if i >= final:
            break

    # N=0 means API after the final/start sequence.
    if api_enabled and api_after == 0 and not api_sent:
        await send_api_result(bot, uid)
        api_sent = True

    if mark_user:
        set_flow_state(uid, True)

    await send_approval(bot, uid)
    return sent_count


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Admin /start opens ONLY the admin panel; it does not run the user flow.
    if uid == ADMIN_ID:
        await update.message.reply_text(
            "👑 PRO BOT ADMIN PANEL 👑",
            reply_markup=main_menu()
        )
        return

    # Telegram deep-link payload arrives here as /start <payload>.
    # It is intentionally hidden from the button label and does not get
    # printed back to the user.
    payload = ""
    if context.args:
        payload = context.args[0].strip()

    add_user(uid)
    await run_start_flow(
        context.bot,
        uid,
        mark_user=True,
        start_payload=payload
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data.clear()
    await update.message.reply_text(
        "👑 PRO BOT ADMIN PANEL 👑",
        reply_markup=main_menu()
    )


async def user_reply_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID or not update.message:
        return

    # Only forward replies after the configured FINAL point.
    if not flow_finished(uid):
        return

    try:
        name = update.effective_user.full_name or "Unknown"
        username = update.effective_user.username
        username_text = f"@{username}" if username else "Not set"
        header = (
            "📩 New User Reply\n\n"
            f"👤 Name: {name}\n"
            f"🆔 UID: {uid}\n"
            f"🔗 Username: {username_text}"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "↩️ Reply to User",
                callback_data=f"reply:{uid}"
            )]
        ])

        await context.bot.send_message(
            ADMIN_ID,
            header,
            reply_markup=keyboard
        )
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
    data = q.data or ""

    # Request "Yes" button: launch the same Start flow as /start.
    if data == "request_yes":
        if uid != ADMIN_ID:
            if get_setting("request_button_action") == "START":
                await q.message.reply_text("⏳ Opening...")
                add_user(uid)
                await run_start_flow(context.bot, uid, mark_user=True)
            else:
                await q.message.reply_text("ℹ️ Button action is OFF.")
        return

    if data == "approval_action":
        return

    if uid != ADMIN_ID:
        return

    if data == "home":
        await q.edit_message_text(
            "👑 PRO BOT ADMIN PANEL 👑",
            reply_markup=main_menu()
        )
    elif data == "stats":
        await q.answer(
            f"Requests: {stat('total_requests')} | Members: {len(users())}",
            show_alert=True
        )
    elif data == "noop":
        return
    elif data == "sync_all":
        a = await asyncio.to_thread(sync_members_to_github)
        b = await asyncio.to_thread(sync_state_to_github)
        await q.answer(
            "✅ Synced" if a and b else "⚠️ Partial/failed sync",
            show_alert=True
        )
    elif data == "request_menu":
        await q.edit_message_text(
            "📥 Request Settings",
            reply_markup=request_menu()
        )
    elif data == "start_menu":
        await q.edit_message_text(
            "▶️ Start Settings",
            reply_markup=start_menu()
        )
    elif data == "api_menu":
        await q.edit_message_text(
            "🌐 API Settings",
            reply_markup=api_menu()
        )
    elif data == "approval_menu":
        await q.edit_message_text(
            "✏️ Approval Settings",
            reply_markup=approval_menu()
        )
    elif data == "req_add":
        context.user_data["state"] = "req_add"
        await q.edit_message_text(
            "📝 Request session ke liye message/media bhejo."
        )
    elif data == "req_clear":
        clear_messages("messages_list")
        await q.edit_message_text(
            "✅ Request messages cleared.",
            reply_markup=request_menu()
        )
    elif data == "req_button":
        context.user_data["state"] = "req_button"
        await q.edit_message_text(
            "🔘 Button text bhejo. `OFF` se button hide hoga."
        )
    elif data == "req_action":
        current = get_setting("request_button_action")
        new = "NONE" if current == "START" else "START"
        set_setting("request_button_action", new)
        await q.edit_message_text(
            f"✅ Button action: {new}",
            reply_markup=request_menu()
        )
    elif data == "req_payload":
        context.user_data["state"] = "req_payload"
        await q.edit_message_text(
            "🔗 Hidden /start payload bhejo.\n"
            "Example: get\n"
            "Blank/0 bhejne se payload clear ho jayega."
        )
    elif data == "req_auto":
        new = "OFF" if get_setting("auto_accept") == "ON" else "ON"
        set_setting("auto_accept", new)
        await q.edit_message_text(
            f"✅ Auto Accept: {new}",
            reply_markup=request_menu()
        )
    elif data == "req_test":
        await send_request_sequence(context.bot, ADMIN_ID)
    elif data == "start_add":
        context.user_data["state"] = "start_add"
        await q.edit_message_text(
            "📝 /start ke liye message/media bhejo."
        )
    elif data == "start_clear":
        clear_messages("start_messages_list")
        await q.edit_message_text(
            "✅ Start messages cleared.",
            reply_markup=start_menu()
        )
    elif data == "start_final":
        context.user_data["state"] = "start_final"
        await q.edit_message_text(
            f"🎯 Final number bhejo (1-{len(START_MESSAGES)})."
        )
    elif data == "start_test":
        await run_start_flow(context.bot, ADMIN_ID, mark_user=False)
    elif data == "api_toggle":
        new = "OFF" if get_setting("api_enabled") == "ON" else "ON"
        set_setting("api_enabled", new)
        await q.edit_message_text(
            f"🌐 API: {new}",
            reply_markup=api_menu()
        )
    elif data == "api_url":
        context.user_data["state"] = "api_url"
        await q.edit_message_text("🔗 Full API URL bhejo.")
    elif data == "api_path":
        context.user_data["state"] = "api_path"
        await q.edit_message_text(
            "📍 JSON path bhejo, example: `data.0.number`.\n"
            "Root ke liye `root`."
        )
    elif data == "api_template":
        context.user_data["state"] = "api_template"
        await q.edit_message_text(
            "📝 Message template bhejo.\n"
            "`{value}` API value ko represent karega.\n"
            "Example: `Period: {value}`"
        )
    elif data == "api_after":
        context.user_data["state"] = "api_after"
        await q.edit_message_text(
            "🔢 Kitne Start messages ke baad API message bhejna hai?\n"
            "`0` = Start sequence ke baad."
        )
    elif data == "api_test":
        await send_api_result(context.bot, ADMIN_ID)
    elif data == "ap_msg":
        context.user_data["state"] = "ap_msg"
        await q.edit_message_text(
            "📝 Approval message/media bhejo."
        )
    elif data == "ap_btn":
        context.user_data["state"] = "ap_btn"
        await q.edit_message_text(
            "🔘 Approval button text bhejo. `OFF` se button hide hoga."
        )
    elif data == "ap_clear":
        clear_approval()
        await q.edit_message_text(
            "✅ Default approval restored.",
            reply_markup=approval_menu()
        )
    elif data.startswith("reply:"):
        try:
            target = int(data.split(":", 1)[1])
        except ValueError:
            await q.answer("Invalid UID", show_alert=True)
            return
        context.user_data["reply_to"] = target
        context.user_data["state"] = "admin_reply"
        await q.message.reply_text(
            f"✍️ UID {target} ko reply bhejo."
        )


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
    elif state == "req_payload":
        t = (m.text or "").strip()
        if t.lower() in ("0", "off", "none", "clear", "blank"):
            t = ""
        elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t):
            await m.reply_text(
                "❌ Invalid payload. Sirf A-Z, a-z, 0-9, _ aur - use karo "
                "(max 64 characters)."
            )
            return
        set_setting("request_start_payload", t)
        context.user_data.clear()
        await m.reply_text(
            "✅ Hidden /start payload saved.",
            reply_markup=request_menu()
        )
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
        set_setting("approval_button", (m.text or "").strip() or "OFF")
        context.user_data.clear()
        await m.reply_text("✅ Approval button saved.", reply_markup=approval_menu())


# ============================================================
# MESSAGE ROUTER
# ============================================================
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    if update.effective_user.id == ADMIN_ID:
        await admin_content(update, context)
    else:
        await user_reply_to_admin(update, context)


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
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("sync_members", sync_command))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, message_router)
    )

    logging.info("PRO BOT ONLINE | ADMIN_ID=%s", ADMIN_ID)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
