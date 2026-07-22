# main.py - TURSO + BACKUP CHANNEL + NO FORWARD TAG - UADMIN PARTITION + NICKNAME + ADVANCED VISIBILITY - POLLING 24/7
# Requirements: pip install Flask python-telegram-bot>=21.4 libsql-experimental python-dotenv
# ENV: BOT_TOKEN, OWNER_ID, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, BACKUP_CHANNEL_ID (-100...), PORT

import os
import re
import random
import string
import asyncio
import json
import uuid
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "").strip() # -1001234567890 private channel

if not BOT_TOKEN or not OWNER_ID:
    print("WARNING: BOT_TOKEN / OWNER_ID missing!")
if not TURSO_URL or not TURSO_TOKEN:
    print("WARNING: TURSO URL / TOKEN missing!")
if not BACKUP_CHANNEL_ID:
    print("WARNING: BACKUP_CHANNEL_ID not set - backup disabled, renew fail hoga!")

# ---------------- TURSO SETUP ----------------
import requests

class TursoCursor:
    def __init__(self, rows):
        self._rows = rows
    def fetchall(self):
        return self._rows
    def fetchone(self):
        return self._rows[0] if self._rows else None

class TursoDB:
    def __init__(self, url, token):
        self.http_url = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.token = token
        self.last_id = None

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
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        r = requests.post(self.http_url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        rows = []
        if "results" in data and len(data["results"])>0:
            res = data["results"][0]
            if res.get("type") == "ok":
                result = res.get("response", {}).get("result", {})
                # last insert id
                if "last_insert_rowid" in result:
                    try:
                        self.last_id = int(result["last_insert_rowid"])
                    except:
                        pass
                raw_rows = result.get("rows", [])
                for row in raw_rows:
                    parsed = []
                    for col in row:
                        t = col.get("type")
                        v = col.get("value")
                        if t == "integer":
                            parsed.append(int(v))
                        elif t == "float":
                            parsed.append(float(v))
                        elif t == "null":
                            parsed.append(None)
                        else:
                            parsed.append(v)
                    rows.append(tuple(parsed))
        return TursoCursor(rows)

    def commit(self):
        pass # HTTP me auto-commit hota hai

db = TursoDB(TURSO_URL, TURSO_TOKEN)

def init_db():
    """Saare tables banata hai agar exist nahi karte"""
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
    db.execute("""CREATE TABLE IF NOT EXISTS access_keys (
        key TEXT PRIMARY KEY,
        is_used INTEGER DEFAULT 0,
        used_by INTEGER,
        nickname TEXT,
        key_type TEXT,
        created_at TEXT,
        created_by INTEGER
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
    db.commit()
    print("✅ Turso tables ready")

init_db()

# ---------------- TELEGRAM SETUP ----------------
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

app = Flask(__name__)

# ---------------- HELPER FUNCTIONS ----------------
def clean_button_text(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r'[^\w\s\-\_\.\(\)\[\]\{\}]+', '', t, flags=re.UNICODE).strip()
    return t

def is_owner(uid):
    return str(uid) == OWNER_ID

def is_co_admin(uid):
    try:
        cur = db.execute("SELECT user_id FROM co_admins WHERE user_id =?", (int(uid),))
        return len(cur.fetchall()) > 0
    except Exception as e:
        print(f"is_co_admin error {e}")
        return False

def is_user_admin(uid):
    try:
        cur = db.execute("SELECT user_id FROM user_admins WHERE user_id =?", (int(uid),))
        return len(cur.fetchall()) > 0
    except Exception as e:
        print(f"is_user_admin error {e}")
        return False

def get_all_user_admins():
    try:
        cur = db.execute("SELECT user_id, nickname, created_by, created_at FROM user_admins ORDER BY created_at DESC")
        rows = cur.fetchall()
        return [{"user_id": r[0], "nickname": r[1], "created_by": r[2], "created_at": r[3]} for r in rows]
    except Exception as e:
        print(f"get_all_user_admins error {e}")
        return []

def get_all_co_admin_ids():
    try:
        cur = db.execute("SELECT user_id FROM co_admins")
        return [int(r[0]) for r in cur.fetchall()]
    except:
        return []

def get_user_admin_ids():
    try:
        return [int(x['user_id']) for x in get_all_user_admins()]
    except:
        return []

def is_authorized(uid):
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
    if is_owner(uid):
        return "owner"
    if is_co_admin(uid):
        return "co_admin"
    if is_user_admin(uid):
        return "user_admin"
    if is_authorized(uid):
        return "normal_user"
    return "unauthorized"

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
        db.commit()
    except Exception as e:
        print(f"set_user_state error {e}")

def clear_user_state(uid):
    try:
        db.execute("DELETE FROM user_states WHERE user_id =?", (int(uid),))
        db.commit()
    except:
        pass

def generate_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"KEY-{rand}"

def generate_uadmin_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"UADMIN-{rand}"

# Auto delete helpers - 15 sec for files, 30 sec for upload msgs
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
    ("🛡️ Co-Owner + Owner Only", "coowner_owner"),
    ("👥 All UAdmins Only", "uadmins_only"),
    ("👤 Specific UAdmin Only", "specific_uadmin"),
    ("👥 Users + Owner Only", "users_owner_only"),
]

def can_view_button(uid, btn, role, user_admin_ids):
    """Main visibility logic - partition + advanced visibility"""
    if role == "owner":
        return True
    created_by = btn.get('created_by')
    vis = btn.get('visibility', 'all')
    visible_to = btn.get('visible_to_user_id')

    # Apna banaya hua button hamesha dikhega
    if created_by and int(created_by) == int(uid):
        return True

    if role == "co_admin":
        if created_by and int(created_by) in user_admin_ids:
            return False # user admin ka button main menu me nahi, sirf User Admins List se
        if vis == "owner_only":
            return False
        if str(vis).startswith("specific_uadmin"):
            return True # co-admin ko manage ke liye dikhega
        return True

    if role == "user_admin":
        # Dusre UAdmin ka private button nahi
        if created_by and int(created_by) in user_admin_ids and int(created_by)!= int(uid):
            return False
        if vis == "all":
            return True
        if vis == "uadmins_only":
            return True
        if str(vis).startswith("specific_uadmin"):
            if visible_to and int(visible_to) == int(uid):
                return True
            return False
        return False

    if role == "normal_user":
        if created_by and int(created_by) in user_admin_ids:
            return False
        if vis in ("all", "users_owner_only"):
            return True
        return False
    return False

def get_buttons_paginated_for_user(uid, page):
    """Main menu ke liye filtered buttons"""
    try:
        cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons ORDER BY name COLLATE NOCASE")
        all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]} for r in cur.fetchall()]
    except Exception as e:
        print(f"get_buttons error {e}")
        return [], 0
    role = get_user_role(uid)
    user_admin_ids = get_user_admin_ids()
    filtered = [b for b in all_btns if can_view_button(uid, b, role, user_admin_ids)]
    total = len(filtered)
    start = page * PER_PAGE
    return filtered[start:start + PER_PAGE], total

def get_manage_buttons_for_user(uid):
    """Manage list ke liye"""
    try:
        cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons ORDER BY name COLLATE NOCASE")
        all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]} for r in cur.fetchall()]
    except:
        return []
    role = get_user_role(uid)
    user_admin_ids = get_user_admin_ids()
    if role == "owner":
        return all_btns
    if role == "co_admin":
        return [b for b in all_btns if not (b.get('created_by') and int(b.get('created_by')) in user_admin_ids)]
    if role == "user_admin":
        return [b for b in all_btns if b.get('created_by') and int(b.get('created_by')) == int(uid)]
    return []

# ---------------- CORE FILE SENDER - NO FORWARD TAG + BACKUP FALLBACK ----------------
async def send_button_files(update, context, button):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    role = get_user_role(uid)

    if not can_view_button(uid, button, role, get_user_admin_ids()):
        msg = await context.bot.send_message(chat_id, "❌ You can't view this button")
        schedule_delete(context.bot, chat_id, msg.message_id)
        return

    try:
        cur = db.execute("SELECT id, file_id, file_type, caption, backup_chat_id, backup_message_id FROM button_files WHERE button_id =? ORDER BY id", (button['id'],))
        files = cur.fetchall()
        if not files:
            msg = await context.bot.send_message(chat_id, f"📭 '{button['name']}' is empty.")
            schedule_delete(context.bot, chat_id, msg.message_id)
            return

        for row in files:
            _fid, file_id, ftype, caption, b_chat, b_mid = row[0], row[1], row[2], row[3], row[4], row[5]
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
                # file_id expire ho gaya (purana bot delete) to backup channel se copy karo - NO FORWARD TAG
                print(f"file_id failed {e}, trying backup copy")
                if b_mid and b_chat and BACKUP_CHANNEL_ID:
                    try:
                        # copy_message se Forwarded From nahi dikhega
                        m = await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(b_chat), message_id=int(b_mid))
                        schedule_delete(context.bot, chat_id, m.message_id)
                    except Exception as e2:
                        await context.bot.send_message(chat_id, f"⚠️ Backup copy failed: {e2}")
                else:
                    await context.bot.send_message(chat_id, "⚠️ File expired & no backup found. Owner ko re-upload karna hoga.")

    except Exception as e:
        await context.bot.send_message(chat_id, f"Error: {e}")

async def show_main_menu(update, context, page=0):
    uid = update.effective_user.id
    buttons, total = get_buttons_paginated_for_user(uid, page)
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
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
            await update.callback_query.answer()
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
    except Exception as e:
        print(e)
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(inline_rows))

async def show_admin_panel(update, context):
    uid = update.effective_user.id
    role = get_user_role(uid)
    if role == "owner":
        kb = [
            [InlineKeyboardButton("🔑 Generate Normal Key", callback_data="admin_gen_key")],
            [InlineKeyboardButton("👑 Generate UAdmin Key", callback_data="admin_gen_uadmin_key")],
            [InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys")],
            [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 Add Co-Admin", callback_data="admin_add_coadmin")],
            [InlineKeyboardButton("📜 List Co-Admins", callback_data="admin_list_coadmin")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
        ]
    elif role == "co_admin":
        kb = [
            [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
        ]
    elif role == "user_admin":
        kb = [
            [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage My Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
        ]
    else:
        await update.effective_message.reply_text("❌ Admin only")
        return
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(f"🛠 Admin Panel - {role}", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.effective_message.reply_text(f"🛠 Admin Panel - {role}", reply_markup=InlineKeyboardMarkup(kb))
    except:
        await update.effective_message.reply_text("🛠 Admin Panel", reply_markup=InlineKeyboardMarkup(kb))

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_authorized(uid):
        clear_user_state(uid)
        await show_main_menu(update, context, 0)
    else:
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Welcome! Send Access Key.\nKEY-XXXX for normal user\nUADMIN-XXXX for User Admin\nContact owner for key.")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    role = get_user_role(uid)

    if data.startswith("view_btn:"):
        _, bid, _ = data.split(":")
        cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons WHERE id =?", (int(bid),))
        r = cur.fetchone()
        if not r:
            await q.answer("Button not found")
            return
        btn = {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]}
        if not can_view_button(uid, btn, role, get_user_admin_ids()):
            await q.answer("❌ No access")
            return
        await q.answer()
        await send_button_files(update, context, btn)

    elif data.startswith("main_page:"):
        page = int(data.split(":")[1])
        await show_main_menu(update, context, page)

    elif data.startswith("vis_"):
        st = get_user_state(uid)
        if not st or st['state']!= "awaiting_new_button_vis":
            await q.answer()
            return
        sdata = st['data']
        vis = data.replace("vis_", "")
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("❌ No UAdmins found. First create UAdmin.")
                return
            rows = []
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"vis_specific_select:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👤 Select UAdmin for Specific:", reply_markup=InlineKeyboardMarkup(rows))
            await q.answer()
            return
        try:
            db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?,?, 'callback',?)",
                       (sdata['name'], vis, int(uid)))
            db.commit()
            await q.edit_message_text(f"✅ Button '{sdata['name']}' created! Vis: {vis}")
        except Exception as e:
            if "UNIQUE" in str(e):
                await q.edit_message_text("❌ Button name already exists! Try different name.")
            else:
                await q.edit_message_text(f"Error: {e}")
        clear_user_state(uid)
        await q.answer()
        await show_main_menu(update, context, 0)

    elif data.startswith("vis_specific_select:"):
        target_id = int(data.split(":")[1])
        st = get_user_state(uid)
        if not st:
            return
        sdata = st['data']
        try:
            db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback',?,?)",
                       (sdata['name'], int(uid), target_id))
            db.commit()
            await q.edit_message_text(f"✅ Button '{sdata['name']}' created for UAdmin {target_id}")
        except Exception as e:
            await q.edit_message_text(f"Error: {e}")
        clear_user_state(uid)
        await q.answer()
        await show_main_menu(update, context, 0)

    elif data.startswith("admin_"):
        if not (is_owner(uid) or is_co_admin(uid) or is_user_admin(uid)):
            await q.answer("Admin only")
            return
        if data == "admin_gen_key":
            if not is_owner(uid):
                await q.answer("Owner only")
                return
            new_key = generate_key()
            db.execute("INSERT INTO access_keys (key, is_used, key_type, created_at) VALUES (?, 0, 'normal',?)",
                       (new_key, datetime.utcnow().isoformat()))
            db.commit()
            await q.edit_message_text(f"✅ Normal Key:\n`{new_key}`\nOne-time use.", parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data == "admin_gen_uadmin_key":
            if not is_owner(uid):
                await q.answer("Owner only")
                return
            new_key = generate_uadmin_key()
            set_user_state(uid, "awaiting_uadmin_nickname", {"key": new_key})
            await q.edit_message_text(f"Generated UAdmin Key:\n`{new_key}`\n\nAb iske liye Nickname bhejo jaise `Rohit Bhai`", parse_mode="Markdown")
        elif data == "admin_list_keys":
            if not is_owner(uid):
                return
            cur = db.execute("SELECT key, is_used, used_by, nickname FROM access_keys ORDER BY created_at DESC LIMIT 20")
            txt = "🔑 Keys (last 20):\n\n"
            for r in cur.fetchall():
                status = "✅ Used" if r[1] else "🟢 Unused"
                txt += f"{r[0]} - {status} by {r[2] or '-'} Nick:{r[3] or '-'}\n"
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data == "admin_add_button":
            set_user_state(uid, "awaiting_new_button_name", {})
            await q.edit_message_text("📝 Send new button NAME (exact name that will show):")
        elif data == "admin_manage_list":
            btns = get_manage_buttons_for_user(uid)
            if not btns:
                await q.edit_message_text("No buttons")
                return
            rows = []
            for b in btns[:30]:
                rows.append([InlineKeyboardButton(b['name'], callback_data=f"manage_btn:{b['id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("🗂 Select button to manage:", reply_markup=InlineKeyboardMarkup(rows))
        elif data == "admin_add_coadmin":
            if not is_owner(uid):
                return
            set_user_state(uid, "awaiting_coadmin_id", {})
            await q.edit_message_text("👥 Send Co-Admin User ID (numeric):")
        elif data == "admin_list_coadmin":
            if not is_owner(uid):
                return
            cur = db.execute("SELECT user_id FROM co_admins")
            txt = "👥 Co-Admins:\n" + "\n".join([f"ID: {r[0]}" for r in cur.fetchall()])
            await q.edit_message_text(txt or "None", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data == "admin_list_uadmins":
            if role not in ("owner", "co_admin"):
                await q.answer("Owner/Co-Owner only")
                return
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("No User Admins", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            rows = []
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"uadmin_view:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👥 User Admins (Nickname):", reply_markup=InlineKeyboardMarkup(rows))
        elif data == "admin_panel":
            await show_admin_panel(update, context)
        await q.answer()

    elif data.startswith("uadmin_view:"):
        target_id = int(data.split(":")[1])
        cur = db.execute("SELECT nickname, created_by FROM user_admins WHERE user_id =?", (target_id,))
        r = cur.fetchone()
        if not r:
            await q.answer("Not found")
            return
        nick = r[0] or "No Nickname"
        kb = [
            [InlineKeyboardButton("📂 View Buttons", callback_data=f"uadmin_view_buttons:{target_id}")],
            [InlineKeyboardButton("✏️ Set Nickname", callback_data=f"uadmin_set_nick:{target_id}")],
            [InlineKeyboardButton("❌ Delete UAdmin", callback_data=f"uadmin_del:{target_id}")],
            [InlineKeyboardButton("Back", callback_data="admin_list_uadmins")],
        ]
        await q.edit_message_text(f"👤 UAdmin: {nick}\nID: {target_id}\nCreated by: {r[1]}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("uadmin_view_buttons:"):
        target_id = int(data.split(":")[1])
        cur = db.execute("SELECT id, name FROM buttons WHERE created_by =?", (target_id,))
        rows = cur.fetchall()
        if not rows:
            await q.edit_message_text(f"No buttons by UAdmin {target_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"uadmin_view:{target_id}")]]))
            return
        kb = [[InlineKeyboardButton(r[1], callback_data=f"manage_btn:{r[0]}")] for r in rows[:30]]
        kb.append([InlineKeyboardButton("Back", callback_data=f"uadmin_view:{target_id}")])
        await q.edit_message_text(f"Buttons by UAdmin {target_id}:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("uadmin_set_nick:"):
        target_id = int(data.split(":")[1])
        set_user_state(uid, "awaiting_set_nickname", {"target_id": target_id})
        await q.edit_message_text(f"✏️ Send new nickname for UAdmin ID {target_id}:")

    elif data.startswith("uadmin_del:"):
        target_id = int(data.split(":")[1])
        if not is_owner(uid):
            await q.answer("Owner only")
            return
        db.execute("DELETE FROM user_admins WHERE user_id =?", (target_id,))
        db.commit()
        await q.edit_message_text(f"✅ UAdmin {target_id} deleted", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

    elif data.startswith("manage_btn:"):
        bid = int(data.split(":")[1])
        rows = [
            [InlineKeyboardButton("📤 Add Files", callback_data=f"m_addfile:{bid}")],
            [InlineKeyboardButton("📄 List/Delete Files", callback_data=f"m_listfiles:{bid}")],
            [InlineKeyboardButton("👁 Visibility", callback_data=f"m_vis:{bid}")],
            [InlineKeyboardButton("❌ Delete Button", callback_data=f"m_delbtn:{bid}")],
            [InlineKeyboardButton("Back", callback_data="admin_manage_list")],
        ]
        await q.edit_message_text(f"Manage Button ID {bid}", reply_markup=InlineKeyboardMarkup(rows))
        await q.answer()

    elif data.startswith("m_addfile:"):
        bid = int(data.split(":")[1])
        set_user_state(uid, "awaiting_file_upload", {"button_id": bid, "upload_msg_ids": [q.message.message_id]})
        await q.edit_message_text(f"📤 Send ANY files for button {bid}.\nFiles auto-delete after Done (30 sec).\nSend multiple, then click ✅ Done below.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]]))
        await q.answer()

    elif data == "m_done_upload":
        st = get_user_state(uid)
        upload_ids = st['data'].get('upload_msg_ids', []) if st and st['data'] else []
        chat_id = q.message.chat_id
        for mid in upload_ids:
            schedule_delete_30(context.bot, chat_id, mid)
        schedule_delete_30(context.bot, chat_id, q.message.message_id)
        clear_user_state(uid)
        m = await q.edit_message_text("✅ Upload finished. All upload messages will disappear in 30 seconds...")
        schedule_delete_30(context.bot, chat_id, m.message_id)
        await q.answer()

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
        db.commit()
        await q.answer("Deleted")
        await q.edit_message_text("Deleted", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

    elif data.startswith("m_delbtn:"):
        bid = int(data.split(":")[1])
        cur = db.execute("SELECT created_by FROM buttons WHERE id =?", (bid,))
        r = cur.fetchone()
        if r and role == "user_admin" and r[0] and int(r[0])!= int(uid):
            await q.answer("❌ Sirf apna button delete kar sakte ho")
            return
        db.execute("DELETE FROM buttons WHERE id =?", (bid,))
        db.commit()
        await q.edit_message_text("✅ Button Deleted")
        await q.answer()

    elif data.startswith("m_vis:"):
        bid = int(data.split(":")[1])
        rows = []
        for name, val in VIS_OPTIONS:
            rows.append([InlineKeyboardButton(name, callback_data=f"m_vis_set:{bid}:{val}")])
        rows.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
        await q.edit_message_text(f"👁 Choose new visibility for {bid}:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("m_vis_set:"):
        parts = data.split(":")
        bid = int(parts[1])
        vis = parts[2]
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("No UAdmins")
                return
            rows = []
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"m_vis_specific:{bid}:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data=f"m_vis:{bid}")])
            await q.edit_message_text("👤 Select UAdmin:", reply_markup=InlineKeyboardMarkup(rows))
            await q.answer()
            return
        db.execute("UPDATE buttons SET visibility =?, visible_to_user_id = NULL WHERE id =?", (vis, bid))
        db.commit()
        await q.answer(f"Vis -> {vis}")
        await q.edit_message_text(f"Visibility changed to {vis}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

    elif data.startswith("m_vis_specific:"):
        _, bid, target_id = data.split(":")
        db.execute("UPDATE buttons SET visibility = 'specific_uadmin', visible_to_user_id =? WHERE id =?", (int(target_id), int(bid)))
        db.commit()
        await q.edit_message_text(f"Visibility -> Specific UAdmin {target_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    state_obj = get_user_state(uid)
    state = state_obj['state'] if state_obj else None
    sdata = state_obj['data'] if state_obj and state_obj.get('data') else {}

    if state == "awaiting_access_key":
        key_input = text.upper().strip()
        cur = db.execute("SELECT key, nickname, key_type FROM access_keys WHERE key =? AND is_used = 0", (key_input,))
        r = cur.fetchone()
        if r:
            is_uadmin_key = key_input.startswith("UADMIN-") or r[2] == 'uadmin'
            db.execute("UPDATE access_keys SET is_used = 1, used_by =? WHERE key =?", (int(uid), key_input))
            db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)", (int(uid), datetime.utcnow().isoformat()))
            if is_uadmin_key:
                nick = r[1] or f"UAdmin-{uid}"
                db.execute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)",
                           (int(uid), nick, int(OWNER_ID), datetime.utcnow().isoformat()))
                db.commit()
                clear_user_state(uid)
                await update.effective_message.reply_text(f"✅ User Admin Access Granted!\nNickname: {nick}\nAb tum apna alag partition use karoge.")
            else:
                db.commit()
                clear_user_state(uid)
                await update.effective_message.reply_text("✅ Access granted!")
            await show_main_menu(update, context, 0)
        else:
            await update.effective_message.reply_text("❌ Invalid or used key. Try again or contact owner.")
        return

    if not is_authorized(uid):
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Send Access Key first. Format KEY-XXXXXXXX / UADMIN-XXXXXXXX")
        return

    if state == "awaiting_uadmin_nickname":
        nick = text
        key = sdata.get('key')
        db.execute("INSERT INTO access_keys (key, is_used, nickname, key_type, created_at) VALUES (?, 0,?, 'uadmin',?)",
                   (key, nick, datetime.utcnow().isoformat()))
        db.commit()
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ UAdmin Key Created:\n`{key}`\nNickname: {nick}\nIsko bhejo, user auto UAdmin ban jayega.", parse_mode="Markdown")
        return

    if state == "awaiting_set_nickname":
        target_id = sdata.get('target_id')
        new_nick = text
        db.execute("UPDATE user_admins SET nickname =? WHERE user_id =?", (new_nick, target_id))
        db.commit()
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ Nickname updated for ID {target_id} -> {new_nick}")
        return

    if state == "awaiting_new_button_name":
        if not text:
            await update.effective_message.reply_text("Send valid name")
            return
        set_user_state(uid, "awaiting_new_button_vis", {"name": text})
        rows = [[InlineKeyboardButton(name, callback_data=f"vis_{val}")] for name, val in VIS_OPTIONS]
        await update.effective_message.reply_text("👁 Choose visibility:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if state == "awaiting_new_button_vis":
        # Fallback if user types text instead of clicking
        vis = "all"
        if "Owner Only" in text:
            vis = "owner_only"
        try:
            db.execute("INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?,?, 'callback',?)",
                       (sdata['name'], vis, int(uid)))
            db.commit()
            await update.effective_message.reply_text(f"✅ Button '{sdata['name']}' created! (Normal)")
        except Exception as e:
            if "UNIQUE" in str(e):
                await update.effective_message.reply_text("❌ Button name already exists!")
            else:
                await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid)
        await show_main_menu(update, context, 0)
        return

    if state == "awaiting_coadmin_id":
        try:
            new_id = int(re.search(r'\d+', text).group())
            db.execute("INSERT OR REPLACE INTO co_admins (user_id, added_by, created_at) VALUES (?,?,?)",
                       (new_id, int(uid), datetime.utcnow().isoformat()))
            db.execute("INSERT OR IGNORE INTO authorized_users (user_id, created_at) VALUES (?,?)",
                       (new_id, datetime.utcnow().isoformat()))
            db.commit()
            await update.effective_message.reply_text(f"✅ Co-Admin added: {new_id}")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e} - send numeric ID")
        clear_user_state(uid)
        return

    if state == "awaiting_file_upload":
        bid = sdata.get('button_id')
        upload_ids = sdata.get('upload_msg_ids', [])
        if text == "✅ Done":
            chat_id = update.effective_chat.id
            for mid in upload_ids:
                schedule_delete_30(context.bot, chat_id, mid)
            clear_user_state(uid)
            m = await update.effective_message.reply_text("✅ Upload finished. All upload messages will disappear in 30 seconds...")
            schedule_delete_30(context.bot, chat_id, m.message_id)
            schedule_delete_30(context.bot, chat_id, update.effective_message.message_id)
            return

        msg = update.effective_message
        upload_ids.append(msg.message_id)

        file_info = None
        if msg.photo:
            p = msg.photo[-1]
            file_info = {"file_id": p.file_id, "file_unique_id": p.file_unique_id, "file_type": "photo", "caption": msg.caption or ""}
        elif msg.document:
            file_info = {"file_id": msg.document.file_id, "file_unique_id": msg.document.file_unique_id, "file_type": "document", "caption": msg.caption or msg.document.file_name or ""}
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
        elif text and text!= "✅ Done":
            file_info = {"file_id": f"text_{uuid.uuid4()}", "file_unique_id": f"textu_{uuid.uuid4()}", "file_type": "text", "caption": text}

        if file_info:
            try:
                # BACKUP TO PRIVATE CHANNEL - NO FORWARD TAG RENEW SYSTEM
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
                        elif file_info['file_type'] == 'audio':
                            bm = await context.bot.send_audio(int(BACKUP_CHANNEL_ID), audio=file_info['file_id'], caption=file_info['caption'] or "")
                        elif file_info['file_type'] == 'voice':
                            bm = await context.bot.send_voice(int(BACKUP_CHANNEL_ID), voice=file_info['file_id'], caption=file_info['caption'] or "")
                        else:
                            bm = await context.bot.copy_message(chat_id=int(BACKUP_CHANNEL_ID), from_chat_id=update.effective_chat.id, message_id=msg.message_id)
                        backup_chat = int(BACKUP_CHANNEL_ID)
                        backup_mid = bm.message_id
                    except Exception as be:
                        print(f"Backup failed: {be}")

                db.execute("INSERT INTO button_files (button_id, file_id, file_unique_id, file_type, caption, backup_chat_id, backup_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (bid, file_info['file_id'], file_info['file_unique_id'], file_info['file_type'], file_info['caption'], backup_chat, backup_mid, datetime.utcnow().isoformat()))
                db.commit()

                kb_done = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]])
                confirm = await update.effective_message.reply_text(f"✅ Added {file_info['file_type']}. Backup: {'Yes' if backup_mid else 'No'}. Niche Done dabao ya aur file bhejo.", reply_markup=kb_done)
                upload_ids.append(confirm.message_id)
                sdata['upload_msg_ids'] = upload_ids
                set_user_state(uid, "awaiting_file_upload", sdata)
            except Exception as e:
                await update.effective_message.reply_text(f"Error saving: {e}")
        return

    # Direct button name search - exact same as old bot
    if text:
        try:
            cur = db.execute("SELECT id, name, visibility, created_by, visible_to_user_id FROM buttons")
            all_btns = [{"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4]} for r in cur.fetchall()]
            matched = None
            for b in all_btns:
                if b['name'].lower().strip() == text.lower().strip() or clean_button_text(b['name']).lower() == clean_button_text(text).lower():
                    matched = b
                    break
            if matched:
                role = get_user_role(uid)
                if not can_view_button(uid, matched, role, get_user_admin_ids()):
                    await update.effective_message.reply_text("❌ Owner only button")
                    return
                await send_button_files(update, context, matched)
                return
        except Exception as e:
            print(e)

# ---------- Telegram App Setup ----------
tg_app = Application.builder().token(BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start_handler))
tg_app.add_handler(CallbackQueryHandler(callback_handler))
tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(tg_app.initialize())
loop.run_until_complete(tg_app.start())

# ---------- Flask Routes ----------
@app.route("/")
def home():
    return "Bot Running - TURSO + BACKUP CHANNEL + NO FORWARD TAG - Polling 24/7 - UptimeRobot OK"

@app.route("/keep-alive")
def keep_alive():
    try:
        db.execute("SELECT 1")
        return jsonify({"status": "ok", "msg": "Turso pinged - backup channel alive"}), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ---------- POLLING 24/7 RUNNER ----------
if __name__ == "__main__":
    def run_flask():
        port = int(os.getenv("PORT", 5000))
        print(f"Flask for UptimeRobot running on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, use_reloader=False)

    threading.Thread(target=run_flask, daemon=True).start()

    async def start_polling():
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            print("Webhook deleted, polling mode...")
        except Exception as e:
            print(f"Delete webhook error: {e}")
        await tg_app.updater.start_polling(drop_pending_updates=True)
        print(f"✅ Polling started! Owner {OWNER_ID} Turso ready. Backup: {BACKUP_CHANNEL_ID}")

    loop.run_until_complete(start_polling())
    loop.run_forever()
