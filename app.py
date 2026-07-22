# main.py - TURSO HTTP (No Rust) + BACKUP CHANNEL + NO FORWARD TAG + OWNER SUPER POWER + UADMIN PARTITION FIX + SPEED CACHE + FULL 950+ LINES
# Requirements: Flask==3.0.3, python-telegram-bot==21.6, python-dotenv==1.0.1, requests==2.32.3
# ENV: BOT_TOKEN, OWNER_ID, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, BACKUP_CHANNEL_ID, PORT

import os
import re
import random
import string
import asyncio
import json
import uuid
import threading
import time
from datetime import datetime

from flask import Flask, jsonify
from dotenv import load_dotenv

# Load env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "").strip()

if not BOT_TOKEN or not OWNER_ID:
    print("WARNING: BOT_TOKEN / OWNER_ID missing!")
if not TURSO_URL or not TURSO_TOKEN:
    print("WARNING: TURSO URL / TOKEN missing!")
if not BACKUP_CHANNEL_ID:
    print("WARNING: BACKUP_CHANNEL_ID not set - backup disabled!")

# ---------------- TURSO HTTP CLIENT - FAST SESSION - NO RUST ----------------
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

app = Flask(__name__)

class TursoCursor:
    """Simple cursor wrapper for Turso HTTP results"""
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None

class TursoDB:
    """Turso HTTP pipeline client - avoids libsql-experimental Rust build error on Render"""
    def __init__(self, url, token):
        # libsql:// -> https:// + /v2/pipeline
        self.http_url = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.token = token
        self.session = requests.Session()

    def _args(self, params):
        args = []
        for p in params:
            if p is None:
                args.append({"type": "null"})
            elif isinstance(p, int):
                args.append({"type": "integer", "value": str(p)})
            elif isinstance(p, float):
                args.append({"type": "float", "value": p})
            else:
                args.append({"type": "text", "value": str(p)})
        return args

    def execute(self, sql, params=()):
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": self._args(params)}},
                {"type": "close"}
            ]
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        r = self.session.post(self.http_url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = []
        try:
            if "results" in data and len(data["results"]) > 0:
                res = data["results"][0]
                if res.get("type") == "ok":
                    result = res.get("response", {}).get("result", {})
                    raw_rows = result.get("rows", [])
                    for row in raw_rows:
                        parsed = []
                        for col in row:
                            t = col.get("type")
                            v = col.get("value")
                            if t == "integer":
                                try:
                                    parsed.append(int(v))
                                except:
                                    parsed.append(v)
                            elif t == "float":
                                parsed.append(float(v))
                            elif t == "null":
                                parsed.append(None)
                            else:
                                parsed.append(v)
                        rows.append(tuple(parsed))
        except Exception as e:
            print(f"Turso parse error: {e} data: {data}")
        return TursoCursor(rows)

    def commit(self):
        # HTTP API is auto-commit, kept for compatibility
        pass

# Initialize Turso DB
db = TursoDB(TURSO_URL, TURSO_TOKEN)

def init_db():
    """Create all tables if not exists - auto runs on boot"""
    db.execute("""CREATE TABLE IF NOT EXISTS co_admins (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_admins (
        user_id INTEGER PRIMARY KEY,
        nickname TEXT,
        created_by INTEGER,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS authorized_users (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS banned_users (
        user_id INTEGER PRIMARY KEY,
        banned_by INTEGER,
        reason TEXT,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS access_keys (
        key TEXT PRIMARY KEY,
        is_used INTEGER DEFAULT 0,
        used_by INTEGER,
        nickname TEXT,
        key_type TEXT,
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT,
        data TEXT,
        updated_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        visibility TEXT DEFAULT 'all',
        btn_type TEXT DEFAULT 'callback',
        color TEXT,
        emoji TEXT,
        created_by INTEGER,
        visible_to_user_id INTEGER
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS button_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        button_id INTEGER,
        file_id TEXT,
        file_unique_id TEXT,
        file_type TEXT,
        caption TEXT,
        backup_chat_id INTEGER,
        backup_message_id INTEGER,
        created_at TEXT,
        FOREIGN KEY(button_id) REFERENCES buttons(id) ON DELETE CASCADE
    )""")
    print("✅ Turso tables ready + banned_users + backup support")

init_db()

# ---------------- SPEED CACHE - FIX SLOW REPLY ----------------
CACHE = {
    "co_ids": [],
    "uadmins": [],
    "ts": 0
}

def refresh_cache(force=False):
    """Refresh co_admin and uadmin cache every 30 sec for speed"""
    now = time.time()
    if not force and now - CACHE["ts"] < 30 and CACHE["uadmins"]:
        return
    try:
        cur = db.execute("SELECT user_id FROM co_admins")
        CACHE["co_ids"] = [int(r[0]) for r in cur.fetchall()]
        cur = db.execute("SELECT user_id, nickname, created_by, created_at FROM user_admins ORDER BY created_at DESC")
        CACHE["uadmins"] = [{"user_id": r[0], "nickname": r[1], "created_by": r[2], "created_at": r[3]} for r in cur.fetchall()]
        CACHE["ts"] = now
    except Exception as e:
        print(f"cache refresh error {e}")

def get_all_user_admins():
    refresh_cache()
    return CACHE["uadmins"]

def get_all_co_admin_ids():
    refresh_cache()
    return CACHE["co_ids"]

def get_user_admin_ids():
    try:
        return [int(x['user_id']) for x in get_all_user_admins()]
    except:
        return []

# ---------------- HELPER FUNCTIONS - SAME AS OLD 950 LINE CODE ----------------
def clean_button_text(text: str) -> str:
    """Clean button text for comparison"""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r'[^\w\s\-\_\.\(\)\[\]\{\}]+', '', t, flags=re.UNICODE).strip()
    return t

def is_owner(uid):
    return str(uid) == OWNER_ID

def is_banned(uid):
    try:
        cur = db.execute("SELECT user_id FROM banned_users WHERE user_id =?", (int(uid),))
        return len(cur.fetchall()) > 0
    except:
        return False

def is_co_admin(uid):
    if is_banned(uid):
        return False
    return int(uid) in get_all_co_admin_ids()

def is_user_admin(uid):
    if is_banned(uid):
        return False
    return int(uid) in get_user_admin_ids()

def is_authorized(uid):
    if is_banned(uid):
        return False
    if is_owner(uid):
        return True
    if is_co_admin(uid):
        return True
    if is_user_admin(uid):
        return True
    try:
        cur = db.execute("SELECT user_id FROM authorized_users WHERE user_id =?", (int(uid),))
        return len(cur.fetchall()) > 0
    except:
        return False

def get_user_role(uid):
    if is_banned(uid):
        return "banned"
    if is_owner(uid):
        return "owner"
    if is_co_admin(uid):
        return "co_admin"
    if is_user_admin(uid):
        return "user_admin"
    if is_authorized(uid):
        return "normal_user"
    return "unauthorized"

def ban_user(target_id, banned_by):
    """Owner super power - ban any user even co-owner, full delete"""
    tid = int(target_id)
    db.execute("DELETE FROM co_admins WHERE user_id =?", (tid,))
    db.execute("DELETE FROM user_admins WHERE user_id =?", (tid,))
    db.execute("DELETE FROM authorized_users WHERE user_id =?", (tid,))
    db.execute("DELETE FROM user_states WHERE user_id =?", (tid,))
    db.execute("INSERT OR REPLACE INTO banned_users (user_id, banned_by, reason, created_at) VALUES (?,?,?,?)",
               (tid, int(banned_by), "banned by owner", datetime.utcnow().isoformat()))
    refresh_cache(force=True)

def unban_user(target_id):
    db.execute("DELETE FROM banned_users WHERE user_id =?", (int(target_id),))
    refresh_cache(force=True)

def get_user_state(uid):
    try:
        cur = db.execute("SELECT state, data FROM user_states WHERE user_id =?", (int(uid),))
        r = cur.fetchone()
        if not r:
            return None
        state, data_json = r[0], r[1]
        data = json.loads(data_json) if data_json else {}
        return {"state": state, "data": data}
    except Exception as e:
        print(f"get_user_state error {e}")
        return None

def set_user_state(uid, state, data=None):
    if data is None:
        data = {}
    try:
        db.execute("INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at) VALUES (?,?,?,?)",
                   (int(uid), state, json.dumps(data), datetime.utcnow().isoformat()))
    except Exception as e:
        print(f"set_user_state error {e}")

def clear_user_state(uid):
    try:
        db.execute("DELETE FROM user_states WHERE user_id =?", (int(uid),))
    except:
        pass

def generate_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"KEY-{rand}"

def generate_uadmin_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"UADMIN-{rand}"

# Auto delete helpers
async def auto_delete_message(bot, chat_id, message_id, delay=15):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

def schedule_delete(bot, chat_id, message_id):
    asyncio.create_task(auto_delete_message(bot, chat_id, message_id, 15))

def schedule_delete_30(bot, chat_id, message_id):
    asyncio.create_task(auto_delete_message(bot, chat_id, message_id, 30))

def build_inline_button(btn):
    return InlineKeyboardButton(text=btn['name'], callback_data=f"view_btn:{btn['id']}:0")

PER_PAGE = 15

VIS_OPTIONS = [
    ("🌍 Public (All)", "all"),
    ("👑 Owner Only", "owner_only"),
    ("🛡 Co-Owner + Owner Only", "coowner_owner"),
    ("👥 All UAdmins Only", "uadmins_only"),
    ("👤 Specific UAdmin Only", "specific_uadmin"),
    ("👥 Users + Owner Only", "users_owner_only"),
]

def can_view_in_main_menu(uid, btn, role, user_admin_ids):
    """Owner main menu me UAdmin buttons nahi dikhenge - separate partition"""
    if role == "owner":
        if btn.get('created_by') and int(btn.get('created_by')) in user_admin_ids:
            return False
        return True
    if role == "co_admin":
        if btn.get('created_by') and int(btn.get('created_by')) in user_admin_ids:
            return False
        return True
    if role == "user_admin":
        # UAdmin ko sirf apna partition dikhega
        return btn.get('created_by') and int(btn.get('created_by')) == int(uid)
    if role == "normal_user":
        if btn.get('created_by') and int(btn.get('created_by')) in user_admin_ids:
            return False
        return btn.get('visibility') in ("all", "users_owner_only")
    return False

def can_access_button(uid, btn, role, user_admin_ids):
    """File access - owner can access all via admin panel"""
    if role == "owner":
        return True
    if role == "co_admin":
        if btn.get('created_by') and int(btn.get('created_by')) in user_admin_ids:
            return False
        return True
    if role == "user_admin":
        return btn.get('created_by') and int(btn.get('created_by')) == int(uid)
    if role == "normal_user":
        if btn.get('created_by') and int(btn.get('created_by')) in user_admin_ids:
            return False
        return btn.get('visibility') in ("all", "users_owner_only")
    return False

def can_view_button(uid, btn, role, user_admin_ids):
    # Compatibility wrapper for old code
    return can_view_in_main_menu(uid, btn, role, user_admin_ids)

def get_buttons_paginated_for_user(uid, page):
    """Main menu filtered buttons"""
    try:
        cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons ORDER BY name COLLATE NOCASE")
        all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]} for r in cur.fetchall()]
    except Exception as e:
        print(f"get_buttons error {e}")
        return [], 0
    role = get_user_role(uid)
    user_admin_ids = get_user_admin_ids()
    filtered = [b for b in all_btns if can_view_in_main_menu(uid, b, role, user_admin_ids)]
    total = len(filtered)
    start = page * PER_PAGE
    return filtered[start:start + PER_PAGE], total

def get_manage_buttons_for_user(uid):
    """Manage list - partition wise"""
    try:
        cur = db.execute("SELECT id, name, visibility, created_by FROM buttons ORDER BY name COLLATE NOCASE")
        all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3]} for r in cur.fetchall()]
    except:
        return []
    role = get_user_role(uid)
    if role == "owner":
        # Owner manage me UAdmin buttons nahi dikhenge, wo alag se UAdmin list me dekhega
        return [b for b in all_btns if b.get('created_by') not in get_user_admin_ids()]
    if role == "co_admin":
        return [b for b in all_btns if b.get('created_by') not in get_user_admin_ids()]
    if role == "user_admin":
        return [b for b in all_btns if b.get('created_by') and int(b.get('created_by')) == int(uid)]
    return []

async def send_button_files(update, context, button):
    """No Forward Tag + Backup Fallback - instant"""
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    role = get_user_role(uid)
    if not can_access_button(uid, button, role, get_user_admin_ids()):
        m = await context.bot.send_message(chat_id, "❌ You can't view this button")
        schedule_delete(context.bot, chat_id, m.message_id)
        return
    try:
        cur = db.execute("SELECT id, file_id, file_type, caption, backup_chat_id, backup_message_id FROM button_files WHERE button_id =? ORDER BY id", (button['id'],))
        files = cur.fetchall()
        if not files:
            m = await context.bot.send_message(chat_id, f"📭 '{button['name']}' is empty.")
            schedule_delete(context.bot, chat_id, m.message_id)
            return
        for row in files:
            _fid, file_id, ftype, caption, b_chat, b_mid = row
            try:
                cap = (caption or "") + "\n\n⏳ Auto-delete 15 sec... Click again to view."
                if ftype == 'text' or not file_id or file_id.startswith('text_'):
                    m = await context.bot.send_message(chat_id, text=caption or "No content")
                elif ftype == 'photo':
                    m = await context.bot.send_photo(chat_id, photo=file_id, caption=cap)
                elif ftype == 'video':
                    m = await context.bot.send_video(chat_id, video=file_id, caption=cap)
                elif ftype == 'audio':
                    m = await context.bot.send_audio(chat_id, audio=file_id, caption=cap)
                elif ftype == 'voice':
                    m = await context.bot.send_voice(chat_id, voice=file_id, caption=cap)
                elif ftype == 'video_note':
                    m = await context.bot.send_video_note(chat_id, video_note=file_id)
                elif ftype == 'sticker':
                    m = await context.bot.send_sticker(chat_id, sticker=file_id)
                else:
                    m = await context.bot.send_document(chat_id, document=file_id, caption=cap)
                schedule_delete(context.bot, chat_id, m.message_id)
            except Exception as e:
                print(f"file_id failed {e}, trying backup copy")
                if b_mid and b_chat and BACKUP_CHANNEL_ID:
                    try:
                        m = await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(b_chat), message_id=int(b_mid))
                        schedule_delete(context.bot, chat_id, m.message_id)
                    except Exception as e2:
                        await context.bot.send_message(chat_id, f"⚠ Backup copy failed: {e2}")
                else:
                    await context.bot.send_message(chat_id, "⚠ File expired & no backup found.")
    except Exception as e:
        await context.bot.send_message(chat_id, f"Error: {e}")

async def show_main_menu(update, context, page=0):
    uid = update.effective_user.id
    if is_banned(uid):
        await context.bot.send_message(update.effective_chat.id, "🚫 You are banned by owner.")
        return
    buttons, total = get_buttons_paginated_for_user(uid, page)
    total_pages = max(1, (total + PER_PAGE - 1)//PER_PAGE)
    inline_rows = []
    r = []
    for b in buttons:
        r.append(build_inline_button(b))
        if len(r) == 2:
            inline_rows.append(r)
            r = []
    if r:
        inline_rows.append(r)
    pag_row = []
    if page > 0:
        pag_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"main_page:{page-1}"))
    if page < total_pages - 1:
        pag_row.append(InlineKeyboardButton("Next ➡", callback_data=f"main_page:{page+1}"))
    if pag_row:
        inline_rows.append(pag_row)
    if is_owner(uid) or is_co_admin(uid) or is_user_admin(uid):
        inline_rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    text = f"📂 Main Menu (Page {page+1}/{total_pages}) - {total} buttons\nSelect any button:"
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
    except:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(inline_rows))

async def show_admin_panel(update, context):
    uid = update.effective_user.id
    role = get_user_role(uid)
    if role == "banned":
        await context.bot.send_message(update.effective_chat.id, "🚫 Banned")
        return
    if role == "owner":
        kb = [
            [InlineKeyboardButton("🔑 Gen Normal Key", callback_data="admin_gen_key"), InlineKeyboardButton("👑 Gen UAdmin Key", callback_data="admin_gen_uadmin_key")],
            [InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys"), InlineKeyboardButton("➕ Add Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 Add Co-Admin", callback_data="admin_add_coadmin"), InlineKeyboardButton("📜 List Co-Admins", callback_data="admin_list_coadmin")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton("🚫 Ban User", callback_data="owner_ban"), InlineKeyboardButton("✅ Unban User", callback_data="owner_unban")],
            [InlineKeyboardButton("📜 Banned List", callback_data="owner_banned_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
    elif role == "co_admin":
        kb = [
            [InlineKeyboardButton("➕ Add Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
    elif role == "user_admin":
        kb = [
            [InlineKeyboardButton("➕ Add Button (My Partition)", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 My Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
    else:
        await update.effective_message.reply_text("❌ Admin only")
        return
    if update.callback_query:
        await update.callback_query.edit_message_text(f"🛠 Admin Panel - {role}", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.effective_message.reply_text(f"🛠 Admin Panel - {role}", reply_markup=InlineKeyboardMarkup(kb))

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        await update.effective_message.reply_text("🚫 You are banned by owner.")
        return
    if is_authorized(uid):
        clear_user_state(uid)
        await show_main_menu(update, context, 0)
    else:
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Welcome! Send Access Key\nKEY-XXXX / UADMIN-XXXX")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    role = get_user_role(uid)
    try:
        await q.answer()
    except:
        pass

    if data.startswith("view_btn:"):
        _, bid, _ = data.split(":")
        cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons WHERE id =?", (int(bid),))
        r = cur.fetchone()
        if not r:
            return
        btn = {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]}
        if not can_access_button(uid, btn, role, get_user_admin_ids()):
            return
        await send_button_files(update, context, btn)

    elif data.startswith("main_page:"):
        await show_main_menu(update, context, int(data.split(":")[1]))

    elif data.startswith("vis_"):
        st = get_user_state(uid)
        if not st or st['state']!= "awaiting_new_button_vis":
            return
        vis = data.replace("vis_", "")
        if role == "user_admin":
            vis = "all"
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            rows = [[InlineKeyboardButton(f"{ua['nickname']} (ID:{ua['user_id']})", callback_data=f"vis_specific_select:{ua['user_id']}")] for ua in uadmins]
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👤 Select UAdmin:", reply_markup=InlineKeyboardMarkup(rows))
            return
        try:
            db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?,?, 'callback',?)", (st['data']['name'], vis, int(uid)))
            await q.edit_message_text(f"✅ Button '{st['data']['name']}' created! Vis: {vis}")
        except Exception as e:
            await q.edit_message_text(f"❌ Exists: {e}")
        clear_user_state(uid)
        await show_main_menu(update, context, 0)

    elif data.startswith("vis_specific_select:"):
        target_id = int(data.split(":")[1])
        st = get_user_state(uid)
        if not st:
            return
        db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback',?,?)", (st['data']['name'], int(uid), target_id))
        await q.edit_message_text(f"✅ Created for UAdmin {target_id}")
        clear_user_state(uid)
        await show_main_menu(update, context, 0)

    elif data.startswith("admin_"):
        if data == "admin_gen_key":
            if not is_owner(uid): return
            k = generate_key()
            db.execute("INSERT INTO access_keys (key, is_used, key_type, created_at) VALUES (?, 0, 'normal',?)", (k, datetime.utcnow().isoformat()))
            await q.edit_message_text(f"✅ Normal Key:\n`{k}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data == "admin_gen_uadmin_key":
            if not is_owner(uid): return
            k = generate_uadmin_key()
            set_user_state(uid, "awaiting_uadmin_nickname", {"key": k})
            await q.edit_message_text(f"UAdmin Key: `{k}`\nAb Nickname bhejo", parse_mode="Markdown")
        elif data == "admin_list_keys":
            cur = db.execute("SELECT key, is_used, used_by, nickname FROM access_keys ORDER BY created_at DESC LIMIT 20")
            txt = "🔑 Keys:\n\n" + "\n".join([f"{r[0]} - {'Used' if r[1] else 'Unused'} by {r[2] or '-'} Nick:{r[3] or '-'}" for r in cur.fetchall()])
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data == "admin_add_button":
            set_user_state(uid, "awaiting_new_button_name", {})
            await q.edit_message_text("📝 Send new button NAME:")
        elif data == "admin_manage_list":
            btns = get_manage_buttons_for_user(uid)
            if not btns:
                await q.edit_message_text("No buttons")
                return
            rows = [[InlineKeyboardButton(b['name'], callback_data=f"manage_btn:{b['id']}")] for b in btns[:30]]
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("🗂 Your Buttons (Partition):", reply_markup=InlineKeyboardMarkup(rows))
        elif data == "admin_add_coadmin":
            if not is_owner(uid): return
            set_user_state(uid, "awaiting_coadmin_id", {})
            await q.edit_message_text("👥 Send Co-Admin User ID:")
        elif data == "admin_list_coadmin":
            cur = db.execute("SELECT user_id FROM co_admins")
            rows = cur.fetchall()
            if not rows:
                await q.edit_message_text("No Co-Admins", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            kb = [[InlineKeyboardButton(f"Co-Admin ID: {r[0]}", callback_data=f"coadmin_view:{r[0]}")] for r in rows]
            kb.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("📜 Co-Admins - click to manage:", reply_markup=InlineKeyboardMarkup(kb))
        elif data == "admin_list_uadmins":
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("No User Admins", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            rows = [[InlineKeyboardButton(f"{ua['nickname'] or 'UAdmin'} (ID:{ua['user_id']})", callback_data=f"uadmin_view:{ua['user_id']}")] for ua in uadmins]
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👥 User Admins - each partition separate:", reply_markup=InlineKeyboardMarkup(rows))
        elif data == "admin_panel":
            await show_admin_panel(update, context)

    elif data.startswith("uadmin_view:"):
        tid = int(data.split(":")[1])
        cur = db.execute("SELECT nickname, created_by FROM user_admins WHERE user_id =?", (tid,))
        r = cur.fetchone()
        if not r:
            await q.edit_message_text("Not found")
            return
        kb = [
            [InlineKeyboardButton("📂 View Buttons (His Partition)", callback_data=f"uadmin_view_buttons:{tid}")],
            [InlineKeyboardButton("✏ Set Nickname", callback_data=f"uadmin_set_nick:{tid}")],
            [InlineKeyboardButton("⬆ Promote to Co-Admin", callback_data=f"uadmin_promote:{tid}")],
            [InlineKeyboardButton("🚫 Ban / Delete", callback_data=f"uadmin_del:{tid}")],
            [InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]
        ]
        await q.edit_message_text(f"👤 UAdmin: {r[0]}\nID: {tid}\nBy: {r[1]}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("coadmin_view:"):
        tid = int(data.split(":")[1])
        kb = [
            [InlineKeyboardButton("⬇ Demote to UAdmin", callback_data=f"coadmin_demote:{tid}")],
            [InlineKeyboardButton("🚫 Ban / Remove", callback_data=f"coadmin_del:{tid}")],
            [InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]
        ]
        await q.edit_message_text(f"👤 Co-Admin ID: {tid}\nOwner can demote or ban even co-owner", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("uadmin_view_buttons:"):
        tid = int(data.split(":")[1])
        cur = db.execute("SELECT id, name FROM buttons WHERE created_by =?", (tid,))
        rows = cur.fetchall()
        if not rows:
            await q.edit_message_text(f"No buttons by UAdmin {tid}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"uadmin_view:{tid}")]]))
            return
        kb = [[InlineKeyboardButton(r[1], callback_data=f"manage_btn:{r[0]}")] for r in rows[:30]]
        kb.append([InlineKeyboardButton("Back", callback_data=f"uadmin_view:{tid}")])
        await q.edit_message_text(f"Buttons by UAdmin {tid} - Owner view:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("uadmin_set_nick:"):
        set_user_state(uid, "awaiting_set_nickname", {"target_id": int(data.split(":")[1])})
        await q.edit_message_text(f"✏ Send new nickname for ID {data.split(':')[1]}:")

    elif data.startswith("uadmin_del:"):
        if not is_owner(uid): return
        tid = int(data.split(":")[1])
        ban_user(tid, uid)
        await q.edit_message_text(f"✅ UAdmin {tid} banned & all access removed", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

    elif data.startswith("uadmin_promote:"):
        if not is_owner(uid): return
        tid = int(data.split(":")[1])
        cur = db.execute("SELECT nickname FROM user_admins WHERE user_id =?", (tid,))
        r = cur.fetchone()
        db.execute("DELETE FROM user_admins WHERE user_id =?", (tid,))
        db.execute("INSERT OR REPLACE INTO co_admins (user_id, added_by, created_at) VALUES (?,?,?)", (tid, int(uid), datetime.utcnow().isoformat()))
        db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)", (tid, datetime.utcnow().isoformat()))
        refresh_cache(force=True)
        await q.edit_message_text(f"✅ Promoted {tid} to Co-Admin", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

    elif data.startswith("coadmin_demote:"):
        if not is_owner(uid): return
        tid = int(data.split(":")[1])
        db.execute("DELETE FROM co_admins WHERE user_id =?", (tid,))
        db.execute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)", (tid, f"UAdmin-{tid}", int(uid), datetime.utcnow().isoformat()))
        db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)", (tid, datetime.utcnow().isoformat()))
        refresh_cache(force=True)
        await q.edit_message_text(f"✅ Demoted {tid} to UAdmin", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]]))

    elif data.startswith("coadmin_del:"):
        if not is_owner(uid): return
        tid = int(data.split(":")[1])
        ban_user(tid, uid)
        await q.edit_message_text(f"✅ Co-Admin {tid} banned & removed", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]]))

    elif data == "owner_ban":
        if not is_owner(uid): return
        set_user_state(uid, "awaiting_ban_id", {})
        await q.edit_message_text("🚫 Send User ID to BAN (co-owner bhi ban hoga):")
    elif data == "owner_unban":
        if not is_owner(uid): return
        set_user_state(uid, "awaiting_unban_id", {})
        await q.edit_message_text("✅ Send User ID to UNBAN:")
    elif data == "owner_banned_list":
        cur = db.execute("SELECT user_id, banned_by FROM banned_users LIMIT 30")
        txt = "🚫 Banned Users:\n" + "\n".join([f"ID: {r[0]} by {r[1]}" for r in cur.fetchall()]) or "None"
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))

    elif data.startswith("manage_btn:"):
        bid = int(data.split(":")[1])
        cur = db.execute("SELECT created_by FROM buttons WHERE id =?", (bid,))
        r = cur.fetchone()
        c_by = r[0] if r else None
        if role == "user_admin":
            if c_by and int(c_by)!= int(uid):
                await q.edit_message_text("❌ Sirf apna button")
                return
            kb = [
                [InlineKeyboardButton("📤 Add Files", callback_data=f"m_addfile:{bid}")],
                [InlineKeyboardButton("📄 List/Delete Files", callback_data=f"m_listfiles:{bid}")],
                [InlineKeyboardButton("❌ Delete Button", callback_data=f"m_delbtn:{bid}")],
                [InlineKeyboardButton("Back", callback_data="admin_manage_list")]
            ]
        else:
            kb = [
                [InlineKeyboardButton("📤 Add Files", callback_data=f"m_addfile:{bid}")],
                [InlineKeyboardButton("📄 List/Delete Files", callback_data=f"m_listfiles:{bid}")],
                [InlineKeyboardButton("👁 Visibility", callback_data=f"m_vis:{bid}")],
                [InlineKeyboardButton("❌ Delete Button", callback_data=f"m_delbtn:{bid}")],
                [InlineKeyboardButton("Back", callback_data="admin_manage_list")]
            ]
        await q.edit_message_text(f"Manage Button ID {bid}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("m_addfile:"):
        set_user_state(uid, "awaiting_file_upload", {"button_id": int(data.split(":")[1]), "upload_msg_ids": [q.message.message_id]})
        await q.edit_message_text(f"📤 Send files for {data.split(':')[1]}. Done dabao.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]]))

    elif data == "m_done_upload":
        st = get_user_state(uid)
        upload_ids = st['data'].get('upload_msg_ids', []) if st else []
        for mid in upload_ids:
            schedule_delete_30(context.bot, q.message.chat_id, mid)
        schedule_delete_30(context.bot, q.message.chat_id, q.message.message_id)
        clear_user_state(uid)
        m = await q.edit_message_text("✅ Upload done. 30 sec me delete...")
        schedule_delete_30(context.bot, q.message.chat_id, m.message_id)

    elif data.startswith("m_listfiles:"):
        bid = int(data.split(":")[1])
        cur = db.execute("SELECT id, file_type FROM button_files WHERE button_id =?", (bid,))
        rows = cur.fetchall()
        if not rows:
            await q.edit_message_text("No files", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
            return
        kb = [[InlineKeyboardButton(f"🗑 {r[1]} {r[0]}", callback_data=f"m_delfile:{r[0]}:{bid}")] for r in rows[:20]]
        kb.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
        await q.edit_message_text(f"Files for {bid}:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("m_delfile:"):
        _, fid, bid = data.split(":")
        db.execute("DELETE FROM button_files WHERE id =?", (int(fid),))
        await q.edit_message_text("Deleted", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
    elif data.startswith("m_delbtn:"):
        bid = int(data.split(":")[1])
        db.execute("DELETE FROM buttons WHERE id =?", (bid,))
        await q.edit_message_text("✅ Deleted")
    elif data.startswith("m_vis:"):
        if role == "user_admin":
            await q.edit_message_text("❌ UAdmin ko visibility change nahi milega")
            return
        bid = int(data.split(":")[1])
        rows = [[InlineKeyboardButton(name, callback_data=f"m_vis_set:{bid}:{val}")] for name, val in VIS_OPTIONS]
        rows.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
        await q.edit_message_text(f"Visibility for {bid}:", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("m_vis_set:"):
        if role == "user_admin":
            return
        _, bid, vis = data.split(":")
        bid = int(bid)
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            rows = [[InlineKeyboardButton(f"{ua['nickname']} (ID:{ua['user_id']})", callback_data=f"m_vis_specific:{bid}:{ua['user_id']}")] for ua in uadmins]
            rows.append([InlineKeyboardButton("Back", callback_data=f"m_vis:{bid}")])
            await q.edit_message_text("Select UAdmin:", reply_markup=InlineKeyboardMarkup(rows))
            return
        db.execute("UPDATE buttons SET visibility =?, visible_to_user_id = NULL WHERE id =?", (vis, bid))
        await q.edit_message_text(f"Vis -> {vis}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
    elif data.startswith("m_vis_specific:"):
        _, bid, target_id = data.split(":")
        db.execute("UPDATE buttons SET visibility = 'specific_uadmin', visible_to_user_id =? WHERE id =?", (int(target_id), int(bid)))
        await q.edit_message_text(f"Vis -> Specific UAdmin {target_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if is_banned(uid):
        await update.effective_message.reply_text("🚫 You are banned")
        return
    state_obj = get_user_state(uid)
    state = state_obj['state'] if state_obj else None
    sdata = state_obj['data'] if state_obj else {}

    if state == "awaiting_access_key":
        key_input = text.upper().strip()
        cur = db.execute("SELECT key, nickname, key_type FROM access_keys WHERE key =? AND is_used = 0", (key_input,))
        r = cur.fetchone()
        if r:
            is_uadmin_key = key_input.startswith("UADMIN-") or r[2] == 'uadmin'
            db.execute("UPDATE access_keys SET is_used = 1, used_by =? WHERE key =?", (int(uid), key_input))
            db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)", (int(uid), datetime.utcnow().isoformat()))
            if is_uadmin_key:
                db.execute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)", (int(uid), r[1] or f"UAdmin-{uid}", int(OWNER_ID), datetime.utcnow().isoformat()))
                refresh_cache(force=True)
                clear_user_state(uid)
                await update.effective_message.reply_text(f"✅ UAdmin Granted Nick:{r[1]}")
            else:
                clear_user_state(uid)
                await update.effective_message.reply_text("✅ Access granted!")
            await show_main_menu(update, context, 0)
        else:
            await update.effective_message.reply_text("❌ Invalid key")
        return

    if not is_authorized(uid):
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Send Access Key")
        return

    if state == "awaiting_uadmin_nickname":
        db.execute("INSERT INTO access_keys (key, is_used, nickname, key_type, created_at) VALUES (?, 0,?, 'uadmin',?)", (sdata.get('key'), text, datetime.utcnow().isoformat()))
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ UAdmin Key `{sdata.get('key')}` Nick:{text}", parse_mode="Markdown")
        return

    if state == "awaiting_set_nickname":
        db.execute("UPDATE user_admins SET nickname =? WHERE user_id =?", (text, sdata.get('target_id')))
        refresh_cache(force=True)
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ Nick -> {text}")
        return

    if state == "awaiting_new_button_name":
        if not text:
            await update.effective_message.reply_text("Valid name bhejo")
            return
        role = get_user_role(uid)
        if role == "user_admin":
            try:
                db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback',?,?)", (text, int(uid), int(uid)))
                await update.effective_message.reply_text(f"✅ Button '{text}' created in your private partition!")
            except Exception as e:
                await update.effective_message.reply_text(f"❌ Exists: {e}")
            clear_user_state(uid)
            await show_main_menu(update, context, 0)
            return
        else:
            set_user_state(uid, "awaiting_new_button_vis", {"name": text})
            rows = [[InlineKeyboardButton(name, callback_data=f"vis_{val}")] for name, val in VIS_OPTIONS]
            await update.effective_message.reply_text("👁 Visibility choose karo:", reply_markup=InlineKeyboardMarkup(rows))
            return

    if state == "awaiting_coadmin_id":
        try:
            nid = int(re.search(r'\d+', text).group())
            db.execute("INSERT OR REPLACE INTO co_admins (user_id, added_by, created_at) VALUES (?,?,?)", (nid, int(uid), datetime.utcnow().isoformat()))
            db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)", (nid, datetime.utcnow().isoformat()))
            refresh_cache(force=True)
            await update.effective_message.reply_text(f"✅ Co-Admin {nid} added")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid)
        return

    if state == "awaiting_ban_id":
        if not is_owner(uid):
            clear_user_state(uid)
            return
        try:
            tid = int(re.search(r'\d+', text).group())
            ban_user(tid, uid)
            await update.effective_message.reply_text(f"✅ Banned {tid} - full access removed")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid)
        return

    if state == "awaiting_unban_id":
        if not is_owner(uid):
            clear_user_state(uid)
            return
        try:
            tid = int(re.search(r'\d+', text).group())
            unban_user(tid)
            await update.effective_message.reply_text(f"✅ Unbanned {tid}")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid)
        return

    if state == "awaiting_file_upload":
        bid = sdata.get('button_id')
        upload_ids = sdata.get('upload_msg_ids', [])
        if text == "✅ Done":
            for mid in upload_ids:
                schedule_delete_30(context.bot, update.effective_chat.id, mid)
            clear_user_state(uid)
            m = await update.effective_message.reply_text("✅ Done 30 sec delete...")
            schedule_delete_30(context.bot, update.effective_chat.id, m.message_id)
            return
        msg = update.effective_message
        upload_ids.append(msg.message_id)
        file_info = None
        if msg.photo:
            p = msg.photo[-1]
            file_info = {"file_id": p.file_id, "file_unique_id": p.file_unique_id, "file_type": "photo", "caption": msg.caption or ""}
        elif msg.document:
            file_info = {"file_id": msg.document.file_id, "file_unique_id": msg.document.file_unique_id, "file_type": "document", "caption": msg.caption or ""}
        elif msg.video:
            file_info = {"file_id": msg.video.file_id, "file_unique_id": msg.video.file_unique_id, "file_type": "video", "caption": msg.caption or ""}
        elif msg.audio:
            file_info = {"file_id": msg.audio.file_id, "file_unique_id": msg.audio.file_unique_id, "file_type": "audio", "caption": msg.caption or ""}
        elif msg.voice:
            file_info = {"file_id": msg.voice.file_id, "file_unique_id": msg.voice.file_unique_id, "file_type": "voice", "caption": msg.caption or ""}
        elif msg.video_note:
            file_info = {"file_id": msg.video_note.file_id, "file_unique_id": msg.video_note.file_unique_id, "file_type": "video_note", "caption": ""}
        elif msg.sticker:
            file_info = {"file_id": msg.sticker.file_id, "file_unique_id": msg.sticker.file_unique_id, "file_type": "sticker", "caption": ""}
        elif text:
            file_info = {"file_id": f"text_{uuid.uuid4()}", "file_unique_id": f"textu_{uuid.uuid4()}", "file_type": "text", "caption": text}
        if file_info:
            backup_chat = None
            backup_mid = None
            if BACKUP_CHANNEL_ID and file_info['file_type']!= 'text':
                try:
                    if file_info['file_type'] == 'photo':
                        bm = await context.bot.send_photo(int(BACKUP_CHANNEL_ID), photo=file_info['file_id'], caption=file_info['caption'] or f"backup btn {bid}")
                    elif file_info['file_type'] == 'video':
                        bm = await context.bot.send_video(int(BACKUP_CHANNEL_ID), video=file_info['file_id'], caption=file_info['caption'] or "")
                    elif file_info['file_type'] == 'document':
                        bm = await context.bot.send_document(int(BACKUP_CHANNEL_ID), document=file_info['file_id'], caption=file_info['caption'] or "")
                    else:
                        bm = await context.bot.copy_message(chat_id=int(BACKUP_CHANNEL_ID), from_chat_id=update.effective_chat.id, message_id=msg.message_id)
                    backup_chat = int(BACKUP_CHANNEL_ID)
                    backup_mid = bm.message_id
                except Exception as be:
                    print(f"backup fail {be}")
            db.execute("INSERT INTO button_files (button_id, file_id, file_unique_id, file_type, caption, backup_chat_id, backup_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (bid, file_info['file_id'], file_info['file_unique_id'], file_info['file_type'], file_info['caption'], backup_chat, backup_mid, datetime.utcnow().isoformat()))
            kb_done = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]])
            confirm = await update.effective_message.reply_text(f"✅ Added {file_info['file_type']} Backup:{'Yes' if backup_mid else 'No'}", reply_markup=kb_done)
            upload_ids.append(confirm.message_id)
            sdata['upload_msg_ids'] = upload_ids
            set_user_state(uid, "awaiting_file_upload", sdata)
        return

    if text:
        try:
            cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons")
            all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]} for r in cur.fetchall()]
            matched = None
            for b in all_btns:
                if b['name'].lower().strip() == text.lower().strip() or clean_button_text(b['name']).lower() == clean_button_text(text).lower():
                    matched = b
                    break
            if matched and can_access_button(uid, matched, get_user_role(uid), get_user_admin_ids()):
                await send_button_files(update, context, matched)
        except Exception as e:
            print(e)

# ---------- TELEGRAM APP ----------
tg_app = Application.builder().token(BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start_handler))
tg_app.add_handler(CallbackQueryHandler(callback_handler))
tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(tg_app.initialize())
loop.run_until_complete(tg_app.start())

@app.route("/")
def home():
    return "Bot Running - Full 950+ Lines - Owner Super Power - UAdmin Partition - Speed Fixed"

@app.route("/keep-alive")
def keep_alive():
    try:
        db.execute("SELECT 1")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    def run_flask():
        port = int(os.getenv("PORT", 5000))
        app.run(host="0.0.0.0", port=port, use_reloader=False)

    threading.Thread(target=run_flask, daemon=True).start()

    async def start_polling():
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except:
            pass
        await tg_app.updater.start_polling(drop_pending_updates=True)
        print(f"✅ Polling fast mode Owner {OWNER_ID} Backup: {BACKUP_CHANNEL_ID}")

    loop.run_until_complete(start_polling())
    loop.run_forever()
