# =============================================================================
# main.py - FINAL 2500+ LINES - NO FLASK - ASYNC TURSO - MEMORY LEAK FIXED
# =============================================================================
import os
import re
import random
import string
import asyncio
import json
import uuid
import time
import signal
import sys
import tempfile
import textwrap
import logging
from datetime import datetime, timezone

from aiohttp import web
from dotenv import load_dotenv

# Load.env file if exists
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ---------------- ENV CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
BACKUP_CHANNEL_ID = os.getenv("BACKUP_CHANNEL_ID", "").strip()

if not BOT_TOKEN or not OWNER_ID:
    logging.warning("BOT_TOKEN / OWNER_ID missing!")

if not TURSO_URL or not TURSO_TOKEN:
    logging.warning("TURSO URL / TOKEN missing!")

if not BACKUP_CHANNEL_ID:
    logging.warning("BACKUP_CHANNEL_ID not set - backup disabled!")

# ---------------- IMPORTS FOR TELEGRAM ----------------
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.error import BadRequest, NetworkError, TimedOut, RetryAfter
import dark_mode

# =============================================================================
# TURSO HTTP CLIENT - FULLY ASYNC VIA HTTPX - NO THREADPOOL BLOCKS
# =============================================================================

class TursoCursor:
    """Wrapper for Turso HTTP results."""
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._rows:
            return self._rows[0]
        return None

class TursoDB:
    def __init__(self, url, token):
        self.http_url = url.replace("libsql://", "https://").rstrip("/") + "/v2/pipeline"
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        self.client = None

    def _get_client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                timeout=httpx.Timeout(30.0)
            )
        return self.client

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

    async def aexecute(self, sql, params=()):
        client = self._get_client()
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": self._args(params)}},
                {"type": "close"}
            ]
        }
        try:
            r = await client.post(self.http_url, json=payload, headers=self.headers)
            r.raise_for_status()
            data = r.json()
            rows = []
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
                                try: parsed.append(int(v))
                                except Exception: parsed.append(v)
                            elif t == "float":
                                parsed.append(float(v))
                            elif t == "null":
                                parsed.append(None)
                            else:
                                parsed.append(v)
                        rows.append(tuple(parsed))
            return TursoCursor(rows)
        except Exception as e:
            logging.error(f"Turso HTTP aexecute error: {e}")
            return TursoCursor([])

    async def close(self):
        if self.client:
            await self.client.aclose()


# Dedicated thread pool just for CPU-heavy PDF operations to not block async loop
import concurrent.futures
PDF_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdf")

PDF_BUILD_SEMAPHORE = asyncio.Semaphore(1)

db = TursoDB(TURSO_URL, TURSO_TOKEN)

async def init_db():
    await db.aexecute("""CREATE TABLE IF NOT EXISTS co_admins (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        created_at TEXT
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS user_admins (
        user_id INTEGER PRIMARY KEY,
        nickname TEXT,
        created_by INTEGER,
        created_at TEXT
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS access_keys (
        key TEXT PRIMARY KEY,
        is_used INTEGER DEFAULT 0,
        used_by INTEGER,
        nickname TEXT,
        key_type TEXT,
        created_at TEXT
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS user_states (
        user_id INTEGER PRIMARY KEY,
        state TEXT,
        data TEXT,
        updated_at TEXT
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS buttons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        visibility TEXT DEFAULT 'all',
        btn_type TEXT DEFAULT 'callback',
        color TEXT,
        emoji TEXT,
        created_by INTEGER,
        visible_to_user_id INTEGER,
        locked INTEGER DEFAULT 0
    )""")

    try: await db.aexecute("ALTER TABLE buttons ADD COLUMN locked INTEGER DEFAULT 0")
    except Exception: pass
    try: await db.aexecute("ALTER TABLE user_admins ADD COLUMN hidden_from_coowner INTEGER DEFAULT 0")
    except Exception: pass
    try: await db.aexecute("ALTER TABLE buttons ADD COLUMN visible_to_user_ids TEXT")
    except Exception: pass
    try: await db.aexecute("ALTER TABLE co_admins ADD COLUMN can_gen_keys INTEGER DEFAULT 0")
    except Exception: pass
    try: await db.aexecute("ALTER TABLE access_keys ADD COLUMN generated_by INTEGER")
    except Exception: pass

    await db.aexecute("""CREATE TABLE IF NOT EXISTS button_files (
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
    await db.aexecute("""CREATE TABLE IF NOT EXISTS button_backup_pdfs (
        button_id INTEGER PRIMARY KEY,
        backup_chat_id INTEGER,
        backup_message_id INTEGER,
        updated_at TEXT,
        FOREIGN KEY(button_id) REFERENCES buttons(id) ON DELETE CASCADE
    )""")
    await db.aexecute("CREATE INDEX IF NOT EXISTS idx_button_files_button_id ON button_files(button_id)")
    await db.aexecute("CREATE INDEX IF NOT EXISTS idx_buttons_created_by ON buttons(created_by)")
    await db.aexecute("CREATE INDEX IF NOT EXISTS idx_buttons_visible_to_user_id ON buttons(visible_to_user_id)")
    await dark_mode.init_db(db)
    logging.info("✅ Turso async tables initialized")

# =============================================================================
# SAFE EDIT 
# =============================================================================

async def safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logging.error(f"safe_edit BadRequest: {e}")
    except Exception as e:
        logging.error(f"safe_edit error: {e}")

# =============================================================================
# CACHE LOGIC
# =============================================================================

CACHE = {
    "co_ids": [],
    "co_ids_set": set(),
    "co_keygen_set": set(),
    "uadmins": [],
    "uadmin_ids": [],
    "uadmin_ids_set": set(),
    "uadmin_hidden_set": set(),
    "ts": 0
}

BUTTON_CACHE = {
    "buttons": [],
    "by_id": {},
    "name_map": {},
    "ts": 0
}

CLICK_GUARD = {}   # rate_key -> last-click timestamp; single dict replaces old USER_LAST_CLICK + PROCESSING
SENDING_LOCKS = set()
BUTTON_LAST_SENT = {}

AUTO_DELETE_SECONDS = 60
CLICK_GUARD_WINDOW = 0.5  # dedup window (covers old 0.3s debounce + old 0.5s in-flight PROCESSING window)

def clear_user_button_locks(uid):
    """uid ke saare per-button locks reset karo (BUTTON_LAST_SENT, CLICK_GUARD keys
    uid:button_id format me hote hai; SENDING_LOCKS uid:chat_id:button_id).
    /start pe call hota hai taki user saare buttons phir se khol sake."""
    prefix = f"{uid}:"
    for d in (BUTTON_LAST_SENT, CLICK_GUARD):
        for k in list(d.keys()):
            if k.startswith(prefix):
                del d[k]
    for k in list(SENDING_LOCKS):
        if k.startswith(prefix):
            SENDING_LOCKS.discard(k)

async def refresh_cache(force=False):
    now = time.time()
    if not force and now - CACHE.get("ts",0) < 120:
        return
    try:
        cur = await db.aexecute("SELECT user_id, can_gen_keys FROM co_admins")
        co_rows = cur.fetchall()
        CACHE["co_ids"] = [int(r[0]) for r in co_rows]
        CACHE["co_ids_set"] = set(CACHE["co_ids"])
        CACHE["co_keygen_set"] = {int(r[0]) for r in co_rows if int(r[1] or 0) == 1}

        cur = await db.aexecute("SELECT user_id, nickname, created_by, created_at, hidden_from_coowner FROM user_admins ORDER BY created_at DESC")
        CACHE["uadmins"] = [
            {"user_id": r[0], "nickname": r[1], "created_by": r[2], "created_at": r[3], "hidden_from_coowner": r[4]}
            for r in cur.fetchall()
        ]
        CACHE["uadmin_ids"] = [int(x['user_id']) for x in CACHE["uadmins"]]
        CACHE["uadmin_ids_set"] = set(CACHE["uadmin_ids"])
        CACHE["uadmin_hidden_set"] = {int(x['user_id']) for x in CACHE["uadmins"] if int(x.get('hidden_from_coowner') or 0) == 1}
        CACHE["ts"] = time.time()
    except Exception as e:
        logging.error(f"cache refresh error {e}")



async def get_all_user_admins():
    await refresh_cache()
    return CACHE["uadmins"]

async def get_all_co_admin_ids():
    await refresh_cache()
    return CACHE["co_ids"]

async def get_user_admin_ids():
    try:
        await refresh_cache()
        return CACHE["uadmin_ids"]
    except Exception as e:
        return []

async def get_user_admin_id_set():
    try:
        await refresh_cache()
        return CACHE["uadmin_ids_set"]
    except Exception as e:
        return set()

async def get_uadmin_hidden_id_set():
    try:
        await refresh_cache()
        return CACHE.get("uadmin_hidden_set", set())
    except Exception as e:
        return set()

async def can_generate_keys(uid):
    if is_owner(uid):
        return True
    try:
        await refresh_cache()
        return int(uid) in CACHE.get("co_keygen_set", set())
    except Exception as e:
        return False



def get_uadmin_nickname(target_uid):
    try: target_uid = int(target_uid)
    except Exception as e: return ""
    for ua in CACHE.get("uadmins", []):
        try:
            if int(ua.get('user_id')) == target_uid:
                return ua.get('nickname') or ""
        except Exception as e: continue
    return ""

def invalidate_button_cache():
    BUTTON_CACHE["buttons"] = []
    BUTTON_CACHE["by_id"] = {}
    BUTTON_CACHE["name_map"] = {}
    BUTTON_CACHE["ts"] = 0

LOCK_EMOJI = "\U0001F512"

def is_locked_button(btn):
    try: return int(btn.get('locked') or 0) == 1
    except Exception as e: return False

def display_button_name(btn):
    name = btn.get('name') or ""
    if is_locked_button(btn) and LOCK_EMOJI not in name:
        return f"{name} {LOCK_EMOJI}"
    return name

def display_admin_button_name(btn):
    name = display_button_name(btn)
    vis = btn.get('visibility', 'all')
    
    SHORT_VIS = {
        "all": "🌐",
        "owner_only": "👑",
        "coowner_owner": "👑C",
        "uadmins_only": "👥",
        "uadmins_coowner": "👥C",
        "specific_uadmin": "👤",
    }
    
    vis_emoji = SHORT_VIS.get(vis, "🌐")
    # UAdmin ne public kiya ho toh 👥 dikhao, owner/co-admin ke liye 🌐 hi rahega
    if vis == "all" and not is_owner_created_button(btn):
        vis_emoji = "👥"
    return f"[{vis_emoji}] {name}"

def is_owner_created_button(btn):
    created_by = btn.get('created_by')
    return created_by is None or is_owner(created_by)

async def get_button_by_id(bid):
    try:
        if BUTTON_CACHE.get("ts", 0) > 0:
            cached = BUTTON_CACHE.get("by_id", {}).get(int(bid))
            if cached:
                return cached
        cur = await db.aexecute(
            "SELECT id, name, visibility, created_by, visible_to_user_id, locked, visible_to_user_ids FROM buttons WHERE id =?",
            (int(bid),)
        )
        r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4], "locked": r[5], "visible_to_user_ids": r[6]}
    except Exception as e:
        logging.error(f"get_button_by_id error {e}")
        return None

async def get_all_buttons_cached(force=False):
    now = time.time()
    if not force and now - BUTTON_CACHE.get("ts",0) < 15:
        return BUTTON_CACHE["buttons"]
    try:
        cur = await db.aexecute("SELECT id, name, visibility, created_by, visible_to_user_id, locked, visible_to_user_ids FROM buttons ORDER BY name COLLATE NOCASE")
        BUTTON_CACHE["buttons"] = [
            {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4], "locked": r[5], "visible_to_user_ids": r[6]}
            for r in cur.fetchall()
        ]
        BUTTON_CACHE["by_id"] = {int(b["id"]): b for b in BUTTON_CACHE["buttons"]}
        BUTTON_CACHE["name_map"] = {}
        for b in BUTTON_CACHE["buttons"]:
            name_lower = (b["name"] or "").lower().strip()
            clean_lower = clean_button_text(b["name"]).lower()
            b["_name_lower"] = name_lower
            b["_clean_name_lower"] = clean_lower
            BUTTON_CACHE["name_map"].setdefault(name_lower, b)
            BUTTON_CACHE["name_map"].setdefault(clean_lower, b)
        BUTTON_CACHE["ts"] = now
    except Exception as e:
        logging.error(f"button cache error {e}")
    return BUTTON_CACHE["buttons"]

# =============================================================================
# HELPER FUNCTIONS 
# =============================================================================

def clean_button_text(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r'[^\w\s\-\_\.\(\)\[\]\{\}]+', '', t, flags=re.UNICODE).strip()
    return t

def is_owner(uid):
    return str(uid) == OWNER_ID

async def is_authorized(uid):
    try:
        uid_int = int(uid)
        if is_owner(uid):
            return True
        await refresh_cache()
        return uid_int in CACHE["co_ids_set"] or uid_int in CACHE["uadmin_ids_set"]
    except Exception as e:
        return False

async def get_user_role(uid):
    try:
        uid_int = int(uid)
        if is_owner(uid):
            return "owner"
        await refresh_cache()
        if uid_int in CACHE["co_ids_set"]:
            return "co_admin"
        if uid_int in CACHE["uadmin_ids_set"]:
            return "user_admin"
        return "unauthorized"
    except Exception as e:
        return "unauthorized"



async def get_user_state(uid):
    try:
        cur = await db.aexecute("SELECT state, data FROM user_states WHERE user_id =?", (int(uid),))
        r = cur.fetchone()
        if not r: return None
        state, data_json = r[0], r[1]
        data = json.loads(data_json) if data_json else {}
        return {"state": state, "data": data}
    except Exception as e:
        logging.error(f"get_user_state error {e}")
        return None

async def set_user_state(uid, state, data=None):
    if data is None: data = {}
    try:
        await db.aexecute(
            "INSERT OR REPLACE INTO user_states (user_id, state, data, updated_at) VALUES (?,?,?,?)",
            (int(uid), state, json.dumps(data), datetime.now(timezone.utc).isoformat())
        )
    except Exception as e:
        logging.error(f"set_user_state error {e}")

async def clear_user_state(uid):
    try: await db.aexecute("DELETE FROM user_states WHERE user_id =?", (int(uid),))
    except Exception as e: pass

def generate_uadmin_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"UADMIN-{rand}"



async def auto_delete_message(bot, chat_id, message_id, delay=None):
    if delay is None:
        delay = AUTO_DELETE_SECONDS
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e: pass

def schedule_delete(bot, chat_id, message_id):
    asyncio.create_task(auto_delete_message(bot, chat_id, message_id, AUTO_DELETE_SECONDS))

def schedule_delete_30(bot, chat_id, message_id):
    asyncio.create_task(auto_delete_message(bot, chat_id, message_id, 5))

# =============================================================================
# UPLOAD CONFIRM DEBOUNCE & NEW UPLOADING STATUS LOGIC
# =============================================================================
PENDING_UPLOAD_CONFIRM = {}
PENDING_PDF_REBUILD = {}

PENDING_UPLOAD_STATUS = {}  # uid -> {mid, count, chat_id}
PENDING_CHAT_ACTION_TASKS = {}  # uid -> asyncio.Task

async def start_chat_action_loop(context, chat_id, uid):
    if uid in PENDING_CHAT_ACTION_TASKS:
        try: PENDING_CHAT_ACTION_TASKS[uid].cancel()
        except Exception: pass
        
    async def _loop():
        while uid in PENDING_UPLOAD_STATUS:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            except Exception:
                pass
            await asyncio.sleep(4)
            
    PENDING_CHAT_ACTION_TASKS[uid] = asyncio.create_task(_loop())

def stop_chat_action_loop(uid):
    if uid in PENDING_CHAT_ACTION_TASKS:
        try: PENDING_CHAT_ACTION_TASKS[uid].cancel()
        except Exception: pass
        del PENDING_CHAT_ACTION_TASKS[uid]

async def update_uploading_status(context, uid, chat_id, new_count):
    if uid not in PENDING_UPLOAD_STATUS:
        return
    
    status = PENDING_UPLOAD_STATUS[uid]
    status['count'] = new_count
    mid = status.get('mid')
    
    if uid not in PENDING_CHAT_ACTION_TASKS:
        await start_chat_action_loop(context, chat_id, uid)
        
    rem = new_count % 3
    if rem == 1:
        dots = "."
        hourglasses = "⏳"
        wait_text = "Please wait"
    elif rem == 2:
        dots = ".."
        hourglasses = "⏳⏳"
        wait_text = "Please wait"
    else: # rem == 0
        dots = "..."
        hourglasses = "⏳⏳⏳"
        wait_text = "Don't close chat"
        
    text = f"⏳ Uploading{dots} {new_count} file{'s' if new_count != 1 else ''} received... {wait_text} {hourglasses}"
    
    try:
        await context.bot.edit_message_text(text=text, chat_id=chat_id, message_id=mid)
    except BadRequest:
        pass
    except RetryAfter:
        pass
    except Exception:
        pass


def cancel_pending_upload_confirm(uid):
    info = PENDING_UPLOAD_CONFIRM.pop(uid, None)
    if info and info.get('task'):
        try: info['task'].cancel()
        except Exception as e: pass

def schedule_upload_confirm(context, uid, chat_id, file_type, backup_ok):
    info = PENDING_UPLOAD_CONFIRM.get(uid)
    if info and info.get('task'):
        try: info['task'].cancel()
        except Exception as e: pass
    else:
        info = {"count": 0, "backup_yes": 0, "backup_no": 0}
    info['count'] += 1
    if backup_ok: info['backup_yes'] += 1
    else: info['backup_no'] += 1
    info['last_type'] = file_type

    async def _send_confirm():
        try: await asyncio.sleep(3.5)
        except asyncio.CancelledError: return
        
        # Cleanup uploading status message before final confirm
        if uid in PENDING_UPLOAD_STATUS:
            st_info = PENDING_UPLOAD_STATUS.pop(uid, None)
            if st_info and st_info.get('mid'):
                try: await context.bot.delete_message(chat_id=st_info['chat_id'], message_id=st_info['mid'])
                except Exception: pass
        stop_chat_action_loop(uid)
        
        data = PENDING_UPLOAD_CONFIRM.get(uid)
        if not data: return
        PENDING_UPLOAD_CONFIRM.pop(uid, None)
        if data['count'] <= 1: text = f"✅ Added {data['last_type']} Backup:{'Yes' if data['backup_yes'] else 'No'}"
        else: text = f"✅ Added {data['count']} files (Backup Yes:{data['backup_yes']} No:{data['backup_no']})"
        kb_done = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]])
        try:
            m = await context.bot.send_message(chat_id, text, reply_markup=kb_done)
            st = await get_user_state(uid)
            if st and st.get('state') == "awaiting_file_upload":
                sdata2 = st['data']
                upload_ids = sdata2.get('upload_msg_ids', [])
                upload_ids.append(m.message_id)
                sdata2['upload_msg_ids'] = upload_ids
                await set_user_state(uid, "awaiting_file_upload", sdata2)
        except Exception as e: logging.error(f"upload confirm send error {e}")

    info['task'] = asyncio.create_task(_send_confirm())
    PENDING_UPLOAD_CONFIRM[uid] = info

def schedule_pdf_rebuild(context, bid, btn):
    prev = PENDING_PDF_REBUILD.get(bid)
    if prev:
        try: prev.cancel()
        except Exception as e: pass
    async def _rebuild():
        try: await asyncio.sleep(4.5)
        except asyncio.CancelledError: return
        PENDING_PDF_REBUILD.pop(bid, None)
        try: await refresh_button_pdf_backup(context, bid, btn)
        except Exception as e: logging.error(f"debounced pdf rebuild error {e}")
    PENDING_PDF_REBUILD[bid] = asyncio.create_task(_rebuild())

PDF_MERGE_TYPES = {"text", "pdf"}
PDF_PAGE_W = 595.28
PDF_PAGE_H = 841.89
PDF_MARGIN = 42
MAX_LEGACY_DOCUMENT_SCAN_MB = int(os.getenv("MAX_LEGACY_DOCUMENT_SCAN_MB", "50") or "50")

def safe_pdf_filename(button_name, bid):
    base = clean_button_text(button_name or "").strip() or f"button_{bid}"
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "_", base)
    base = re.sub(r"\s+", " ", base).strip(" ._")
    if not base: base = f"button_{bid}"
    return f"{base[:70]}.pdf"

def backup_caption_with_button(caption, button_name, limit=1024):
    suffix = f"Button: {button_name or 'Unknown'}"
    base = (caption or "").strip()
    if base:
        room = max(0, limit - len(suffix) - 2)
        if len(base) > room:
            base = base[:max(0, room - 3)].rstrip() + "..."
        return f"{base}\n\n{suffix}"
    return suffix[:limit]

async def get_button_pdf_backup(bid):
    try:
        cur = await db.aexecute(
            "SELECT backup_chat_id, backup_message_id FROM button_backup_pdfs WHERE button_id =?",
            (int(bid),)
        )
        return cur.fetchone()
    except Exception as e:
        logging.error(f"get pdf backup error {e}")
        return None

async def delete_button_pdf_backup(context, bid):
    old = await get_button_pdf_backup(bid)
    if old and old[0] and old[1]:
        try: await context.bot.delete_message(chat_id=int(old[0]), message_id=int(old[1]))
        except Exception as e: logging.error(f"old pdf delete skipped {e}")
    try: await db.aexecute("DELETE FROM button_backup_pdfs WHERE button_id =?", (int(bid),))
    except Exception as e: logging.error(f"pdf backup row delete error {e}")

def _pdf_text_lines(text, width=82):
    lines = []
    for raw in (text or "").replace("\r", "").split("\n"):
        if not raw:
            lines.append("")
            continue
        wrapped = textwrap.wrap(raw, width=width, replace_whitespace=False, drop_whitespace=False)
        lines.extend(wrapped or [""])
    return lines

def _find_pdf_font_path():
    env_path = os.getenv("PDF_FONT_PATH", "").strip()
    candidates = [
        env_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\mangal.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None

def _build_button_pdf_reportlab(button_name, entries, pdf_path):
    try:
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception as e:
        return False
    font_name = "Helvetica"
    font_path = _find_pdf_font_path()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("BackupUnicode", font_path))
            font_name = "BackupUnicode"
        except Exception as e: logging.error(f"pdf font register skipped {e}")
    c = canvas.Canvas(pdf_path, pagesize=(PDF_PAGE_W, PDF_PAGE_H))
    c.setTitle(button_name or "Button Backup")
    first = True
    def start_page(title):
        nonlocal first
        if not first: c.showPage()
        first = False
        c.setFont(font_name, 15)
        c.drawString(PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, title[:120])
        c.setFont(font_name, 11)
    for idx, entry in enumerate(entries, start=1):
        title = f"{idx}. {button_name or 'Button'} - {entry['type']}"
        if entry["type"] == "text":
            start_page(title)
            y = PDF_PAGE_H - PDF_MARGIN - 32
            for line in _pdf_text_lines(entry.get("text") or ""):
                if y < PDF_MARGIN:
                    c.showPage()
                    c.setFont(font_name, 11)
                    y = PDF_PAGE_H - PDF_MARGIN
                c.drawString(PDF_MARGIN, y, line[:180])
                y -= 15
        elif entry["type"] == "photo":
            start_page(title)
            y = PDF_PAGE_H - PDF_MARGIN - 28
            caption_lines = _pdf_text_lines(entry.get("caption") or "", width=78)[:8]
            for line in caption_lines:
                c.drawString(PDF_MARGIN, y, line[:170])
                y -= 14
            if caption_lines: y -= 8
            try:
                img = ImageReader(entry["path"])
                iw, ih = img.getSize()
                max_w = PDF_PAGE_W - (2 * PDF_MARGIN)
                max_h = max(120, y - PDF_MARGIN)
                scale = min(max_w / float(iw), max_h / float(ih))
                draw_w = iw * scale
                draw_h = ih * scale
                x = (PDF_PAGE_W - draw_w) / 2
                c.drawImage(img, x, PDF_MARGIN, width=draw_w, height=draw_h)
            except Exception as e:
                c.drawString(PDF_MARGIN, y, f"Photo could not be rendered: {e}")
    if first:
        start_page(button_name or "Button Backup")
        c.drawString(PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN - 32, "No text/photo found.")
    c.save()
    return True

def merge_pdf_parts(parts, output_path):
    if len(parts) == 1:
        try:
            import shutil
            shutil.copyfile(parts[0], output_path)
            return True
        except Exception as e:
            logging.error(f"single pdf copy failed {e}")
            return False
    try:
        from pypdf import PdfWriter
    except Exception as e:
        logging.error(f"pypdf missing, uploaded pdf merge skipped: {e}")
        return False
    writer = PdfWriter()
    for part in parts:
        try: writer.append(part)
        except Exception as e: logging.error(f"pdf append skipped {part}: {e}")
    if not writer.pages:
        return False
    with open(output_path, "wb") as out:
        writer.write(out)
    try: writer.close()
    except Exception as e: pass
    return True

def is_pdf_upload(msg):
    doc = getattr(msg, "document", None)
    if not doc: return False
    mime = (getattr(doc, "mime_type", "") or "").lower()
    name = (getattr(doc, "file_name", "") or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf")

def looks_like_pdf_path(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception as e:
        return False

def should_scan_legacy_document_pdf(tg_file):
    tg_path = (getattr(tg_file, "file_path", "") or "").lower()
    if ".pdf" in tg_path: return True
    try: size = int(getattr(tg_file, "file_size", 0) or 0)
    except Exception as e: size = 0
    if not size: return True
    return size <= MAX_LEGACY_DOCUMENT_SCAN_MB * 1024 * 1024

def _jpeg_info(path):
    with open(path, "rb") as f:
        data = f.read(2)
        if data != b"\xff\xd8": raise ValueError("not a jpeg")
        while True:
            marker_start = f.read(1)
            if not marker_start: break
            if marker_start != b"\xff": continue
            marker = f.read(1)
            while marker == b"\xff": marker = f.read(1)
            code = marker[0]
            if code in (0xD8, 0xD9): continue
            length_bytes = f.read(2)
            if len(length_bytes) != 2: break
            length = int.from_bytes(length_bytes, "big")
            if code in (0xC0, 0xC1, 0xC2, 0xC3):
                chunk = f.read(length - 2)
                height = int.from_bytes(chunk[1:3], "big")
                width = int.from_bytes(chunk[3:5], "big")
                comps = chunk[5]
                return width, height, comps
            f.seek(length - 2, os.SEEK_CUR)
    raise ValueError("jpeg size not found")

def _pdf_stream(data):
    return b"<< /Length " + str(len(data)).encode("ascii") + b" >>\nstream\n" + data + b"\nendstream"

def _pdf_hex_text(text):
    raw = ("\ufeff" + (text or "")).encode("utf-16-be", errors="replace")
    return b"<" + raw.hex().encode("ascii") + b">"

def _manual_pdf_text_ops(lines, x, y, size=11, leading=15):
    ops = [f"BT /F1 {size} Tf {x:.2f} {y:.2f} Td {leading} TL\n".encode("ascii")]
    for line in lines:
        ops.append(_pdf_hex_text(line))
        ops.append(b" Tj T*\n")
    ops.append(b"ET\n")
    return b"".join(ops)

def _build_button_pdf_manual(button_name, entries, pdf_path):
    objects = {}
    page_ids = []
    next_id = 5

    def add_obj(data):
        nonlocal next_id
        oid = next_id
        objects[oid] = data
        next_id += 1
        return oid

    def add_page(content, xobject_part=b""):
        content_id = add_obj(_pdf_stream(content))
        page_id = add_obj(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] "
            b"/Resources << /Font << /F1 3 0 R >> " + xobject_part + b" >> "
            b"/Contents " + str(content_id).encode("ascii") + b" 0 R >>"
        )
        page_ids.append(page_id)

    if not entries:
        lines = [button_name or "Button Backup", "", "No text/photo found."]
        add_page(_manual_pdf_text_ops(lines, PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, 13, 17))

    for idx, entry in enumerate(entries, start=1):
        heading = f"{idx}. {button_name or 'Button'} - {entry['type']}"
        if entry["type"] == "text":
            lines = [heading, ""] + _pdf_text_lines(entry.get("text") or "", width=78)
            chunk = []
            y = PDF_PAGE_H - PDF_MARGIN
            for line in lines:
                chunk.append(line)
                y -= 15
                if y < PDF_MARGIN:
                    add_page(_manual_pdf_text_ops(chunk, PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, 11, 15))
                    chunk = []
                    y = PDF_PAGE_H - PDF_MARGIN
            if chunk:
                add_page(_manual_pdf_text_ops(chunk, PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, 11, 15))
        elif entry["type"] == "photo":
            try:
                width, height, comps = _jpeg_info(entry["path"])
                with open(entry["path"], "rb") as f:
                    img_bytes = f.read()
                color = b"/DeviceGray" if comps == 1 else (b"/DeviceCMYK" if comps == 4 else b"/DeviceRGB")
                image_id = add_obj(
                    b"<< /Type /XObject /Subtype /Image /Width " + str(width).encode("ascii") +
                    b" /Height " + str(height).encode("ascii") + b" /ColorSpace " + color +
                    b" /BitsPerComponent 8 /Filter /DCTDecode /Length " + str(len(img_bytes)).encode("ascii") +
                    b" >>\nstream\n" + img_bytes + b"\nendstream"
                )
                caption_lines = [heading, ""] + _pdf_text_lines(entry.get("caption") or "", width=78)[:8]
                text_block = _manual_pdf_text_ops(caption_lines, PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, 11, 15)
                text_height = max(45, len(caption_lines) * 15 + 18)
                max_w = PDF_PAGE_W - (2 * PDF_MARGIN)
                max_h = PDF_PAGE_H - (2 * PDF_MARGIN) - text_height
                scale = min(max_w / float(width), max_h / float(height))
                draw_w = width * scale
                draw_h = height * scale
                x = (PDF_PAGE_W - draw_w) / 2
                y = PDF_MARGIN
                img_ops = f"q {draw_w:.2f} 0 0 {draw_h:.2f} {x:.2f} {y:.2f} cm /Im{idx} Do Q\n".encode("ascii")
                xobjs = b"/XObject << /Im" + str(idx).encode("ascii") + b" " + str(image_id).encode("ascii") + b" 0 R >>"
                add_page(text_block + img_ops, xobjs)
            except Exception as e:
                lines = [heading, "", f"Photo could not be rendered: {e}"]
                add_page(_manual_pdf_text_ops(lines, PDF_MARGIN, PDF_PAGE_H - PDF_MARGIN, 11, 15))

    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = b" ".join(str(pid).encode("ascii") + b" 0 R" for pid in page_ids)
    objects[2] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_ids)).encode("ascii") + b" >>"
    objects[3] = b"<< /Type /Font /Subtype /Type0 /BaseFont /NotoSans-Regular /Encoding /Identity-H /DescendantFonts [4 0 R] >>"
    objects[4] = b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NotoSans-Regular /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /CIDToGIDMap /Identity /DW 1000 >>"

    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (max(objects) + 1)
        for oid in range(1, max(objects) + 1):
            offsets[oid] = f.tell()
            f.write(str(oid).encode("ascii") + b" 0 obj\n")
            f.write(objects[oid])
            f.write(b"\nendobj\n")
        xref = f.tell()
        f.write(b"xref\n0 " + str(max(objects) + 1).encode("ascii") + b"\n")
        f.write(b"0000000000 65535 f \n")
        for oid in range(1, max(objects) + 1):
            f.write(f"{offsets[oid]:010d} 00000 n \n".encode("ascii"))
        f.write(b"trailer\n<< /Size " + str(max(objects) + 1).encode("ascii") + b" /Root 1 0 R >>\n")
        f.write(b"startxref\n" + str(xref).encode("ascii") + b"\n%%EOF\n")
    return True

def build_button_pdf(button_name, entries, pdf_path):
    if _build_button_pdf_reportlab(button_name, entries, pdf_path):
        return True
    return _build_button_pdf_manual(button_name, entries, pdf_path)

async def _refresh_button_pdf_backup_impl(context, bid, btn=None):
    if not BACKUP_CHANNEL_ID: return
    btn = btn or await get_button_by_id(bid)
    if not btn: return
    try:
        cur = await db.aexecute(
            "SELECT id, file_id, file_type, caption FROM button_files WHERE button_id =? AND file_type IN ('text','pdf','document') ORDER BY id",
            (int(bid),)
        )
        rows = cur.fetchall()
    except Exception as e:
        logging.error(f"pdf rows fetch error {e}")
        return

    if not rows:
        await delete_button_pdf_backup(context, bid)
        return

    button_name = btn.get("name") or f"button_{bid}"
    old = await get_button_pdf_backup(bid)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = []
            pdf_parts = []
            for idx, (_fid, file_id, ftype, caption) in enumerate(rows, start=1):
                if ftype == "text":
                    entries.append({"type": "text", "text": caption or ""})
                elif ftype in ("pdf", "document") and file_id:
                    try:
                        tg_file = await context.bot.get_file(file_id)
                        if ftype == "document" and not should_scan_legacy_document_pdf(tg_file):
                            logging.info(f"legacy document pdf scan skipped for button {bid} file {_fid}: file too large")
                            continue
                        source_pdf = os.path.join(tmpdir, f"source_{idx}.pdf")
                        await tg_file.download_to_drive(custom_path=source_pdf)
                        if looks_like_pdf_path(source_pdf):
                            pdf_parts.append(source_pdf)
                            if ftype == "document":
                                try: await db.aexecute("UPDATE button_files SET file_type = 'pdf' WHERE id =?", (int(_fid),))
                                except Exception as e: logging.error(f"legacy pdf type update skipped {e}")
                    except Exception as e: logging.error(f"uploaded pdf download skipped {e}")

            if not entries and not pdf_parts:
                await delete_button_pdf_backup(context, bid)
                return

            pdf_name = safe_pdf_filename(button_name, bid)
            pdf_path = os.path.join(tmpdir, pdf_name)
            generated_pdf = os.path.join(tmpdir, "generated_text.pdf")
            pdf_loop = asyncio.get_running_loop()
            parts = []
            if entries:
                await pdf_loop.run_in_executor(PDF_EXECUTOR, build_button_pdf, button_name, entries, generated_pdf)
                parts.append(generated_pdf)
            parts.extend(pdf_parts)
            merged_ok = await pdf_loop.run_in_executor(PDF_EXECUTOR, merge_pdf_parts, parts, pdf_path)
            if not merged_ok:
                await pdf_loop.run_in_executor(PDF_EXECUTOR, build_button_pdf, button_name, entries, pdf_path)
            caption = backup_caption_with_button("Text/uploaded PDF backup", button_name)
            with open(pdf_path, "rb") as pdf_file:
                sent = await context.bot.send_document(
                    chat_id=int(BACKUP_CHANNEL_ID),
                    document=pdf_file,
                    filename=pdf_name,
                    caption=caption
                )

        try:
            await db.aexecute(
                "INSERT OR REPLACE INTO button_backup_pdfs (button_id, backup_chat_id, backup_message_id, updated_at) VALUES (?,?,?,?)",
                (int(bid), int(BACKUP_CHANNEL_ID), int(sent.message_id), datetime.now(timezone.utc).isoformat())
            )
        except Exception as e:
            logging.error(f"pdf backup row update failed {e}")
            try: await context.bot.delete_message(chat_id=int(BACKUP_CHANNEL_ID), message_id=int(sent.message_id))
            except Exception as de: logging.error(f"new orphan pdf delete skipped {de}")
            return
        if old and old[0] and old[1]:
            try:
                if int(old[0]) != int(BACKUP_CHANNEL_ID) or int(old[1]) != int(sent.message_id):
                    await context.bot.delete_message(chat_id=int(old[0]), message_id=int(old[1]))
            except Exception as e: logging.error(f"old pdf delete skipped {e}")
    except Exception as e:
        logging.error(f"pdf rebuild fail {e}")

async def refresh_button_pdf_backup(context, bid, btn=None):
    async with PDF_BUILD_SEMAPHORE:
        await _refresh_button_pdf_backup_impl(context, bid, btn)

def build_inline_button(btn, role="user"):
    if role in ("owner", "co_admin", "user_admin"):
        name = display_admin_button_name(btn)
    else:
        name = display_button_name(btn)
    return InlineKeyboardButton(text=name, callback_data=f"open_btn:{btn['id']}:0")

PER_PAGE = 15
VIS_OPTIONS = [
    ("🌍 Public (All)", "all"),
    ("👑 Owner Only", "owner_only"),
    ("🛡 Co-Owner + Owner Only", "coowner_owner"),
    ("👥 All UAdmins Only", "uadmins_only"),
    ("👥 UAdmins + Co-Owner", "uadmins_coowner"),
    ("👤 Specific UAdmin Only", "specific_uadmin"),
]
VIS_LABELS = {val: name for name, val in VIS_OPTIONS}

def get_button_visible_uadmin_ids(btn):
    ids = set()
    if not btn: return ids
    single = btn.get('visible_to_user_id')
    if single:
        try: ids.add(int(single))
        except Exception as e: pass
    multi = btn.get('visible_to_user_ids')
    if multi:
        for part in str(multi).split(','):
            part = part.strip()
            if part:
                try: ids.add(int(part))
                except Exception as e: pass
    return ids

def format_visibility_mode(btn):
    vis = btn.get('visibility', 'all') if btn else 'all'
    label = VIS_LABELS.get(vis, vis)
    if vis == "specific_uadmin" and btn:
        ids = get_button_visible_uadmin_ids(btn)
        if ids:
            parts = []
            for i in sorted(ids):
                nick = get_uadmin_nickname(i)
                parts.append(f"{i} ({nick})" if nick else str(i))
            return f"{label} (ID:{', '.join(parts)})"
    return label

def visible_vis_options_for_role(role):
    if role == "co_admin":
        return [(name, val) for name, val in VIS_OPTIONS if val != "owner_only"]
    return VIS_OPTIONS

def render_uadmin_multiselect_kb(uadmins, selected_ids, toggle_prefix, confirm_callback, back_callback):
    rows = []
    for ua in uadmins:
        try: uid_ = int(ua['user_id'])
        except Exception as e: continue
        mark = "✅ " if uid_ in selected_ids else "◻ "
        rows.append([InlineKeyboardButton(f"{mark}{ua.get('nickname') or 'UAdmin'} (ID:{uid_})", callback_data=f"{toggle_prefix}{uid_}")])
    rows.append([InlineKeyboardButton(f"✅ Confirm ({len(selected_ids)} selected)", callback_data=confirm_callback)])
    rows.append([InlineKeyboardButton("Back", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)

def can_view_in_main_menu(uid, btn, role, user_admin_ids, uadmin_hidden_ids=None):
    uadmin_hidden_ids = uadmin_hidden_ids or set()
    vis = btn.get('visibility', 'all')
    if role == "owner": return True
    if role == "co_admin":
        created_by = btn.get('created_by')
        if created_by and int(created_by) in uadmin_hidden_ids: return False
        return vis != "owner_only"
    if role == "user_admin":
        if vis in ("all", "uadmins_only", "uadmins_coowner"): return True
        if vis == "specific_uadmin": return int(uid) in get_button_visible_uadmin_ids(btn)
        return False
    return False

def can_access_button(uid, btn, role, user_admin_ids, uadmin_hidden_ids=None):
    uadmin_hidden_ids = uadmin_hidden_ids or set()
    if role == "owner": return True
    if role == "co_admin":
        vis = btn.get('visibility', 'all')
        created_by = btn.get('created_by')
        if created_by and int(created_by) in uadmin_hidden_ids: return False
        return vis != "owner_only"
    if role == "user_admin":
        vis = btn.get('visibility', 'all')
        if vis in ("all", "uadmins_only", "uadmins_coowner"): return True
        if vis == "specific_uadmin": return int(uid) in get_button_visible_uadmin_ids(btn)
        return False
    return False

async def can_access_button_async(uid, btn, role, user_admin_ids=None):
    if not btn: return False
    user_admin_ids = user_admin_ids if user_admin_ids is not None else await get_user_admin_ids()
    uadmin_hidden_ids = await get_uadmin_hidden_id_set() if role == "co_admin" else set()
    return can_access_button(uid, btn, role, user_admin_ids, uadmin_hidden_ids)

def can_view_button(uid, btn, role, user_admin_ids):
    return can_view_in_main_menu(uid, btn, role, user_admin_ids)

async def get_buttons_paginated_for_user(uid, page, role=None):
    all_btns = await get_all_buttons_cached()
    role = role or await get_user_role(uid)
    user_admin_ids = await get_user_admin_ids()
    uadmin_hidden_ids = await get_uadmin_hidden_id_set() if role == "co_admin" else set()
    filtered = [b for b in all_btns if can_view_in_main_menu(uid, b, role, user_admin_ids, uadmin_hidden_ids)]
    total = len(filtered)
    start = page * PER_PAGE
    return filtered[start:start + PER_PAGE], total

async def get_manage_buttons_for_user(uid):
    all_btns = await get_all_buttons_cached()
    role = await get_user_role(uid)
    uadmin_id_set = await get_user_admin_id_set()
    if role == "owner":
        return [b for b in all_btns if b.get('created_by') not in uadmin_id_set]
    if role == "co_admin":
        return [b for b in all_btns if b.get('created_by') not in uadmin_id_set]
    if role == "user_admin":
        return [b for b in all_btns if b.get('created_by') and int(b.get('created_by')) == int(uid)]
    return []

async def can_open_manage_button(uid, btn, role):
    if not btn: return False
    created_by = btn.get('created_by')
    if role == "owner": return True
    if role == "co_admin":
        if created_by and int(created_by) in await get_user_admin_id_set():
            return await can_access_button_async(uid, btn, role)
        return True
    if role == "user_admin":
        return created_by and int(created_by) == int(uid)
    return False

async def can_add_files_to_button(uid, btn, role):
    if not await can_open_manage_button(uid, btn, role) or is_locked_button(btn): return False
    if role == "owner": return True
    if role == "co_admin": return True
    if role == "user_admin": return btn.get('created_by') and int(btn.get('created_by')) == int(uid)
    return False

async def can_edit_button(uid, btn, role):
    if not await can_open_manage_button(uid, btn, role) or is_locked_button(btn): return False
    if role == "owner": return True
    if role == "co_admin":
        return not is_owner_created_button(btn) and not (btn.get('created_by') and int(btn.get('created_by')) in await get_user_admin_id_set())
    if role == "user_admin": return btn.get('created_by') and int(btn.get('created_by')) == int(uid)
    return False

async def can_change_visibility(uid, btn, role):
    if not btn: return False
    if not await can_open_manage_button(uid, btn, role) or is_locked_button(btn): return False
    if role == "owner": return True
    if role == "co_admin":
        created_by = btn.get('created_by')
        if created_by and int(created_by) in await get_user_admin_id_set():
            hidden_ids = await get_uadmin_hidden_id_set()
            return int(created_by) not in hidden_ids
        return await can_edit_button(uid, btn, role)
    # user_admin: can only set their OWN button public (all) - not other visibility options
    if role == "user_admin":
        return btn.get('created_by') and int(btn.get('created_by')) == int(uid)
    return False

async def can_uadmin_make_public(uid, btn, role):
    """Check if a user_admin can set their own folder to public (all)."""
    if role != "user_admin": return False
    if not btn or is_locked_button(btn): return False
    if not (btn.get('created_by') and int(btn.get('created_by')) == int(uid)): return False
    # Only show the button if folder is NOT already public
    return btn.get('visibility', 'all') != 'all'

async def show_manage_button_menu(update, context, bid, role=None, back_callback="admin_manage_list"):
    q = update.callback_query
    uid = update.effective_user.id
    role = role or await get_user_role(uid)
    btn = await get_button_by_id(bid)
    if not btn: return
    if not await can_open_manage_button(uid, btn, role):
        await safe_edit(q, "Access denied")
        return
    kb = [[InlineKeyboardButton("\U0001F441 View Files / Data", callback_data=f"view_btn:{bid}:0")]]
    if is_locked_button(btn):
        if role == "owner":
            kb.append([InlineKeyboardButton("\U0001F513 Unlock Folder", callback_data=f"m_lock_toggle:{bid}")])
    else:
        if await can_add_files_to_button(uid, btn, role):
            kb.append([InlineKeyboardButton("\U0001F4E4 Add Files", callback_data=f"m_addfile:{bid}")])
        if await can_edit_button(uid, btn, role):
            kb.append([InlineKeyboardButton("\U0001F4C4 List/Delete Files", callback_data=f"m_listfiles:{bid}")])
            if await can_change_visibility(uid, btn, role) and role != "user_admin":
                kb.append([InlineKeyboardButton("\U0001F441 Visibility", callback_data=f"m_vis:{bid}")])
            kb.append([InlineKeyboardButton("\U0000274C Delete Folder", callback_data=f"m_delbtn:{bid}")])
        # UAdmin: show Make Public button for their own non-public folders
        if await can_uadmin_make_public(uid, btn, role):
            kb.append([InlineKeyboardButton("\U0001F310 Make Public (All can see)", callback_data=f"m_make_public:{bid}")])
        if role == "owner":
            kb.append([InlineKeyboardButton(f"{LOCK_EMOJI} Lock Folder", callback_data=f"m_lock_toggle:{bid}")])
    kb.append([InlineKeyboardButton("Back", callback_data=back_callback)])
    await safe_edit(q, f"Visibility: {format_visibility_mode(btn)}\nManage Folder: {display_admin_button_name(btn)} (ID {bid})", InlineKeyboardMarkup(kb))
# ---------------- TEMP MESSAGE HELPERS ----------------

async def _send_temp(bot, chat_id, text):
    try: return await bot.send_message(chat_id, text)
    except Exception: return None

async def _del_temp(bot, msg):
    if not msg: return
    try: await bot.delete_message(msg.chat.id, msg.message_id)
    except Exception: pass

# ---------------- FILE SENDER ----------------

async def send_button_files(update, context, button):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    send_key = f"{uid}:{chat_id}:{button['id']}"
    if send_key in SENDING_LOCKS:
        return
    rate_key = f"{uid}:{button['id']}"
    now = time.time()
    last_sent = BUTTON_LAST_SENT.get(rate_key, 0)
    if now - last_sent < AUTO_DELETE_SECONDS:
        return
    BUTTON_LAST_SENT[rate_key] = now
    if len(BUTTON_LAST_SENT) > 3000:
        try:
            for kk in list(BUTTON_LAST_SENT.keys())[:1000]:
                del BUTTON_LAST_SENT[kk]
        except: pass
    SENDING_LOCKS.add(send_key)
    if len(SENDING_LOCKS) > 500:
        try: SENDING_LOCKS.clear()
        except: pass
    role = await get_user_role(uid)

    if not await can_access_button_async(uid, button, role):
        m = await context.bot.send_message(chat_id, "❌ You can't view this button")
        schedule_delete(context.bot, chat_id, m.message_id)
        SENDING_LOCKS.discard(send_key)
        return

    temp_msg = await _send_temp(context.bot, chat_id, "📤 Uploading your files... ⏳\nPlease wait, don't spam")
    try: await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
    except: pass

    try:
        cur = await db.aexecute(
            "SELECT id, file_id, file_type, caption, backup_chat_id, backup_message_id FROM button_files WHERE button_id =? ORDER BY id",
            (button['id'],)
        )
        files = cur.fetchall()
        header = await context.bot.send_message(chat_id, f"Visibility: {format_visibility_mode(button)}")
        schedule_delete(context.bot, chat_id, header.message_id)

        if not files:
            m = await context.bot.send_message(chat_id, f"📭 '{button['name']}' is empty.")
            schedule_delete(context.bot, chat_id, m.message_id)
            SENDING_LOCKS.discard(send_key)
            return

        has_merged_pdf_data = any((row[2] in PDF_MERGE_TYPES) for row in files)
        if has_merged_pdf_data:
            pdf_sent = await send_button_merged_pdf(update, context, button)
            if not pdf_sent:
                m = await context.bot.send_message(chat_id, "PDF abhi ready nahi hai. Thodi der baad dobara try karo.")
                schedule_delete(context.bot, chat_id, m.message_id)

        for row in files:
            _fid, file_id, ftype, caption, b_chat, b_mid = row
            if ftype in PDF_MERGE_TYPES:
                continue

            cap = (caption or "") + "\n\n⏳ Auto-delete 15 sec... Click again to view."

            async def _send_item():
                if ftype == 'text' or not file_id or file_id.startswith('text_'):
                    return await context.bot.send_message(chat_id, text=caption or "No content")
                elif ftype == 'photo': return await context.bot.send_photo(chat_id, photo=file_id, caption=cap)
                elif ftype == 'video': return await context.bot.send_video(chat_id, video=file_id, caption=cap)
                elif ftype == 'audio': return await context.bot.send_audio(chat_id, audio=file_id, caption=cap)
                elif ftype == 'voice': return await context.bot.send_voice(chat_id, voice=file_id, caption=cap)
                elif ftype == 'video_note': return await context.bot.send_video_note(chat_id, video_note=file_id)
                elif ftype == 'sticker': return await context.bot.send_sticker(chat_id, sticker=file_id)
                else: return await context.bot.send_document(chat_id, document=file_id, caption=cap)

            try:
                try:
                    m = await _send_item()
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    m = await _send_item()
                schedule_delete(context.bot, chat_id, m.message_id)

            except Exception as e:
                logging.error(f"file_id failed {e}, trying backup copy")
                if b_mid and b_chat and BACKUP_CHANNEL_ID:
                    try:
                        try:
                            m = await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(b_chat), message_id=int(b_mid))
                        except RetryAfter as re:
                            await asyncio.sleep(re.retry_after + 1)
                            m = await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(b_chat), message_id=int(b_mid))
                        schedule_delete(context.bot, chat_id, m.message_id)
                    except Exception as e2:
                        await context.bot.send_message(chat_id, f"⚠ Backup copy failed: {e2}")
                else:
                    await context.bot.send_message(chat_id, "⚠ File expired & no backup found.")

            await asyncio.sleep(0.35)
    except Exception as e:
        await context.bot.send_message(chat_id, f"Error: {e}")
    finally:
        await _del_temp(context.bot, temp_msg)
        SENDING_LOCKS.discard(send_key)

async def send_button_merged_pdf(update, context, button):
    if not BACKUP_CHANNEL_ID: return False
    chat_id = update.effective_chat.id
    bid = int(button["id"])

    temp_msg = await _send_temp(context.bot, chat_id, "📄 Preparing your PDF... ⏳")
    try: await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
    except: pass

    async def copy_cached():
        row = await get_button_pdf_backup(bid)
        if not row or not row[0] or not row[1]: return None
        try:
            try:
                return await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(row[0]), message_id=int(row[1]))
            except RetryAfter as re:
                await asyncio.sleep(re.retry_after + 1)
                return await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(row[0]), message_id=int(row[1]))
        except Exception as e:
            logging.error(f"cached merged pdf copy failed {e}")
            try: await db.aexecute("DELETE FROM button_backup_pdfs WHERE button_id =?", (bid,))
            except Exception as de: logging.error(f"stale pdf backup row cleanup skipped {de}")
            return None

    try:
        copied = await copy_cached()
        if not copied:
            await refresh_button_pdf_backup(context, bid, button)
            copied = await copy_cached()
        if copied:
            schedule_delete(context.bot, chat_id, copied.message_id)
            return True
    except Exception as e:
        logging.error(f"merged pdf send failed {e}")
    finally:
        await _del_temp(context.bot, temp_msg)
        
    return False

dark_mode_controller = dark_mode.DarkMode(
    db=db,
    owner_id=OWNER_ID,
    backup_channel_id=BACKUP_CHANNEL_ID,
    get_user_role=get_user_role,
    is_authorized=is_authorized,
    get_all_user_admins=get_all_user_admins,
    get_manage_buttons_for_user=get_manage_buttons_for_user,
    can_add_files_to_button=can_add_files_to_button,
    get_button_by_id=get_button_by_id,
    get_all_buttons_cached=get_all_buttons_cached,
    set_user_state=set_user_state,
    clear_user_state=clear_user_state,
    invalidate_button_cache=invalidate_button_cache,
    backup_caption_with_button=backup_caption_with_button,
    refresh_button_pdf_backup=refresh_button_pdf_backup,
    schedule_pdf_rebuild=schedule_pdf_rebuild,
    pdf_merge_types=PDF_MERGE_TYPES,
)

# ---------------- MENUS ----------------

async def show_main_menu(update, context, page=0):
    uid = update.effective_user.id
    role = await get_user_role(uid)


    buttons, total = await get_buttons_paginated_for_user(uid, page, role)
    total_pages = max(1, (total + PER_PAGE - 1)//PER_PAGE)

    inline_rows = []
    r = []
    for b in buttons:
        r.append(build_inline_button(b, role))
        if len(r) == 2:
            inline_rows.append(r)
            r = []
    if r: inline_rows.append(r)

    pag_row = []
    if page > 0: pag_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"main_page:{page-1}"))
    if page < total_pages - 1: pag_row.append(InlineKeyboardButton("Next ➡", callback_data=f"main_page:{page+1}"))
    if pag_row: inline_rows.append(pag_row)

    if role in ("owner", "co_admin", "user_admin"):
        inline_rows.append([InlineKeyboardButton("➕ Create New Folder", callback_data="admin_add_button")])
        inline_rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])

    text = f"📂 Main Menu (Page {page+1}/{total_pages}) - {total} folders\nSelect any folder:"

    try:
        if update.callback_query:
            await safe_edit(update.callback_query, text, InlineKeyboardMarkup(inline_rows))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
    except Exception as e:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(inline_rows))

async def show_admin_panel(update, context):
    uid = update.effective_user.id
    role = await get_user_role(uid)


    if role == "owner":
        kb = [
            [InlineKeyboardButton("👑 Gen UAdmin Key", callback_data="admin_gen_uadmin_key"), InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys")],
            [InlineKeyboardButton("➕ Add Folder", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Folders", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 Add Co-Admin", callback_data="admin_add_coadmin"), InlineKeyboardButton("📜 List Co-Admins", callback_data="admin_list_coadmin")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton(await dark_mode_controller.owner_panel_label(), callback_data="dm:panel"), InlineKeyboardButton("Dark Mode Commands", callback_data="dm:perms")],
            [InlineKeyboardButton("🔄 Rebuild PDF (Single)", callback_data="rebuild_single_prompt"), InlineKeyboardButton("🔄 Rebuild All PDFs", callback_data="rebuild_all_pdfs")],
            [InlineKeyboardButton("♻ Shutdown / Restart", callback_data="owner_shutdown")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
    elif role == "co_admin":
        kb = [
            [InlineKeyboardButton("➕ Add Folder", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Folders", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 User Admins List", callback_data="admin_list_uadmins")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
        if await can_generate_keys(uid):
            kb.insert(0, [InlineKeyboardButton("👑 Gen UAdmin Key", callback_data="admin_gen_uadmin_key")])
            kb.insert(1, [InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys")])
    elif role == "user_admin":
        kb = [
            [InlineKeyboardButton("➕ Add Folder (My Partition)", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 My Folders", callback_data="admin_manage_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")]
        ]
    else:
        await update.effective_message.reply_text("❌ Admin only")
        return

    if update.callback_query:
        await safe_edit(update.callback_query, f"🛠 Admin Panel - {role}", InlineKeyboardMarkup(kb))
    else:
        await update.effective_message.reply_text(f"🛠 Admin Panel - {role}", reply_markup=InlineKeyboardMarkup(kb))

# ---------------- HANDLERS ----------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_user_button_locks(uid)
    if await is_authorized(uid):
        await clear_user_state(uid)
        await show_main_menu(update, context, 0)
    else:
        await set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Welcome! Send Access Key\nUADMIN-XXXX")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    role = await get_user_role(uid)
    key = None

        # FINAL OPTIMIZED: Har button ka alag lock
    if data.startswith("open_btn:") or data.startswith("view_btn:"):
        now = time.time()
        try:
            bid_tmp = data.split(":")[1]
        except:
            bid_tmp = "0"
        rate_key = f"{uid}:{bid_tmp}"
        # NOTE: 60-sec AUTO_DELETE_SECONDS lock ab yaha nahi lagta - kyunki open_btn
        # admins ke liye sirf manage-menu bhi khol sakta hai (file send nahi hoti).
        # Ye lock ab send_button_files() ke andar lagta hai, sirf jab file actually bheji jaaye.
        # Single click-guard: dedups rapid re-clicks AND blocks concurrent
        # re-processing of the same button while the first click is still running.
        last_click = CLICK_GUARD.get(rate_key, 0)
        if now - last_click < CLICK_GUARD_WINDOW:
            try: await q.answer()
            except: pass
            return
        CLICK_GUARD[rate_key] = now
        key = rate_key
        if len(CLICK_GUARD) > 3000:
            try:
                for k in list(CLICK_GUARD.keys())[:1000]:
                    del CLICK_GUARD[k]
            except:
                pass

    try:
        try: await q.answer()
        except Exception as e: pass

        if await dark_mode_controller.handle_callback(update, context): return

        if data.startswith("open_btn:"):
            _, bid, _ = data.split(":")
            btn = await get_button_by_id(bid)
            if not btn: return
            if not await can_access_button_async(uid, btn, role): return
            if role in ("owner", "co_admin", "user_admin") and await can_open_manage_button(uid, btn, role):
                await show_manage_button_menu(update, context, int(bid), role, "main_page:0")
                return
            await send_button_files(update, context, btn)

        elif data.startswith("view_btn:"):
            _, bid, _ = data.split(":")
            btn = await get_button_by_id(bid)
            if not btn: return
            if not await can_access_button_async(uid, btn, role): return
            await send_button_files(update, context, btn)

        elif data.startswith("main_page:"):
            await show_main_menu(update, context, int(data.split(":")[1]))

        elif data.startswith("vis_"):
            st = await get_user_state(uid)
            if not st or st['state']!= "awaiting_new_button_vis": return
            vis = data.replace("vis_", "")
            if role == "user_admin": vis = "all"
            if vis == "specific_uadmin":
                uadmins = await get_all_user_admins()
                await set_user_state(uid, "awaiting_new_button_vis_multi", {"name": st['data']['name'], "selected": []})
                rows = render_uadmin_multiselect_kb(uadmins, set(), "vis_multi_toggle:", "vis_multi_confirm", "admin_panel")
                await safe_edit(q, "👤 Select UAdmin(s) - tap to toggle, then Confirm:", rows)
                return
            try:
                await db.aexecute("INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?,?, 'callback',?)", (st['data']['name'], vis, int(uid)))
                invalidate_button_cache()
                await safe_edit(q, f"✅ Folder '{st['data']['name']}' created! Vis: {vis}")
            except Exception as e:
                await safe_edit(q, f"❌ Exists: {e}")
            await clear_user_state(uid)
            await show_main_menu(update, context, 0)

        elif data.startswith("vis_specific_select:"):
            target_id = int(data.split(":")[1])
            st = await get_user_state(uid)
            if not st: return
            await db.aexecute("INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback',?,?)", (st['data']['name'], int(uid), target_id))
            invalidate_button_cache()
            await safe_edit(q, f"✅ Created for UAdmin {target_id}")
            await clear_user_state(uid)
            await show_main_menu(update, context, 0)

        elif data.startswith("vis_multi_toggle:"):
            target_id = int(data.split(":")[1])
            st = await get_user_state(uid)
            if not st or st['state']!= "awaiting_new_button_vis_multi": return
            selected = set(st['data'].get('selected', []))
            if target_id in selected: selected.discard(target_id)
            else: selected.add(target_id)
            st['data']['selected'] = list(selected)
            await set_user_state(uid, "awaiting_new_button_vis_multi", st['data'])
            uadmins = await get_all_user_admins()
            rows = render_uadmin_multiselect_kb(uadmins, selected, "vis_multi_toggle:", "vis_multi_confirm", "admin_panel")
            await safe_edit(q, "👤 Select UAdmin(s) - tap to toggle, then Confirm:", rows)

        elif data == "vis_multi_confirm":
            st = await get_user_state(uid)
            if not st or st['state']!= "awaiting_new_button_vis_multi": return
            selected = st['data'].get('selected', [])
            if not selected:
                await safe_edit(q, "⚠ Kam se kam 1 UAdmin select karo")
                return
            ids_csv = ",".join(str(x) for x in selected)
            first_id = selected[0]
            try:
                await db.aexecute(
                    "INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id, visible_to_user_ids) VALUES (?, 'specific_uadmin', 'callback',?,?,?)",
                    (st['data']['name'], int(uid), int(first_id), ids_csv)
                )
                invalidate_button_cache()
                await safe_edit(q, f"✅ Created for {len(selected)} UAdmin(s)")
            except Exception as e:
                await safe_edit(q, f"❌ Exists: {e}")
            await clear_user_state(uid)
            await show_main_menu(update, context, 0)

        elif data.startswith("admin_"):
            if data == "admin_gen_uadmin_key":
                if not await can_generate_keys(uid): return
                k = generate_uadmin_key()
                await set_user_state(uid, "awaiting_uadmin_nickname", {"key": k})
                await safe_edit(q, f"UAdmin Key: `{k}`\nAb Nickname bhejo")
            elif data == "admin_list_keys":
                if not await can_generate_keys(uid): return
                cur = await db.aexecute("SELECT key, is_used, used_by, nickname, generated_by FROM access_keys ORDER BY created_at DESC LIMIT 20")
                txt = "🔑 Keys:\n\n" + "\n".join([f"{r[0]} - {'Used' if r[1] else 'Unused'} by {r[2] or '-'} | Nick:{r[3] or '-'} | Gen by:{r[4] or OWNER_ID}" for r in cur.fetchall()])
                await safe_edit(q, txt, InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
            elif data == "admin_add_button":
                await set_user_state(uid, "awaiting_new_button_name", {})
                await safe_edit(q, "📝 Send new folder NAME:")
            elif data == "admin_manage_list":
                btns = await get_manage_buttons_for_user(uid)
                if not btns:
                    if role == "owner": await safe_edit(q, "Owner ke apne folders nahi hai. UAdmins ke folders dekhne ke liye User Admins List me jao.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                    else: await safe_edit(q, "No folders", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                    return
                rows = [[InlineKeyboardButton(display_admin_button_name(b), callback_data=f"manage_btn:{b['id']}")] for b in btns[:30]]
                rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
                await safe_edit(q, "🗂 Your Folders (Partition):", InlineKeyboardMarkup(rows))
            elif data == "admin_add_coadmin":
                if not is_owner(uid): return
                await set_user_state(uid, "awaiting_coadmin_id", {})
                await safe_edit(q, "👥 Send Co-Admin User ID:")
            elif data == "admin_list_coadmin":
                cur = await db.aexecute("SELECT user_id FROM co_admins")
                rows = cur.fetchall()
                if not rows:
                    await safe_edit(q, "No Co-Admins", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                    return
                kb = [[InlineKeyboardButton(f"Co-Admin ID: {r[0]}", callback_data=f"coadmin_view:{r[0]}")] for r in rows]
                kb.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
                await safe_edit(q, "📜 Co-Admins - click to manage:", InlineKeyboardMarkup(kb))
            elif data == "admin_list_uadmins":
                uadmins = await get_all_user_admins()
                if role == "co_admin":
                    hidden_ids = await get_uadmin_hidden_id_set()
                    uadmins = [ua for ua in uadmins if int(ua['user_id']) not in hidden_ids]
                if not uadmins:
                    await safe_edit(q, "No User Admins", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                    return
                rows = [[InlineKeyboardButton(f"{ua['nickname'] or 'UAdmin'} (ID:{ua['user_id']})", callback_data=f"uadmin_view:{ua['user_id']}")] for ua in uadmins]
                rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
                await safe_edit(q, "👥 User Admins - each partition separate:", InlineKeyboardMarkup(rows))
            elif data == "admin_panel":
                await show_admin_panel(update, context)

        elif data.startswith("uadmin_view:"):
            tid = int(data.split(":")[1])
            if role == "co_admin":
                hidden_ids = await get_uadmin_hidden_id_set()
                if tid in hidden_ids:
                    await safe_edit(q, "Not found")
                    return
            cur = await db.aexecute("SELECT nickname, created_by, hidden_from_coowner FROM user_admins WHERE user_id =?", (tid,))
            r = cur.fetchone()
            if not r:
                await safe_edit(q, "Not found")
                return
            hide_flag = int(r[2] or 0)
            kb = [
                [InlineKeyboardButton("📂 View Folders (His Partition)", callback_data=f"uadmin_view_buttons:{tid}")],
                [InlineKeyboardButton("✏ Set Nickname", callback_data=f"uadmin_set_nick:{tid}")],
            ]
            extra_line = ""
            if role == "owner":
                hide_label = "👁 Show to Co-Owners" if hide_flag else "🙈 Hide from Co-Owners"
                kb.append([InlineKeyboardButton("⬆ Promote to Co-Admin", callback_data=f"uadmin_promote:{tid}")])
                kb.append([InlineKeyboardButton(hide_label, callback_data=f"uadmin_hide_toggle:{tid}")])
                kb.append([InlineKeyboardButton("🚫 Revoke Access", callback_data=f"uadmin_revoke:{tid}")])
                kb.append([InlineKeyboardButton("☠️ Kick & Delete Data", callback_data=f"uadmin_kick_prompt:{tid}")])
                extra_line = "\n🙈 Hidden from Co-Owners" if hide_flag else ""
            kb.append([InlineKeyboardButton("Back", callback_data="admin_list_uadmins")])
            await safe_edit(q, f"👤 UAdmin: {r[0]}\nID: {tid}\nBy: {r[1]}{extra_line}", InlineKeyboardMarkup(kb))

        elif data.startswith("uadmin_revoke:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM user_admins WHERE user_id =?", (tid,))
            await db.aexecute("DELETE FROM user_states WHERE user_id =?", (tid,))
            await refresh_cache(force=True)
            await safe_edit(q, f"✅ UAdmin {tid} access revoked (data safe).", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

        elif data.startswith("uadmin_kick_prompt:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            kb = [
                [InlineKeyboardButton("✅ Confirm Kick", callback_data=f"uadmin_kick_confirm:{tid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"uadmin_view:{tid}")]
            ]
            await safe_edit(q, f"⚠ ARE YOU SURE?\nUser {tid} ka access aur saare folders delete ho jayenge permanently!", InlineKeyboardMarkup(kb))

        elif data.startswith("uadmin_kick_confirm:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM buttons WHERE created_by =?", (tid,))
            await db.aexecute("DELETE FROM user_admins WHERE user_id =?", (tid,))
            await db.aexecute("DELETE FROM user_states WHERE user_id =?", (tid,))
            invalidate_button_cache()
            await refresh_cache(force=True)
            await safe_edit(q, f"☠️ UAdmin {tid} and their data completely deleted.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

        elif data.startswith("coadmin_view:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            cur = await db.aexecute("SELECT can_gen_keys FROM co_admins WHERE user_id =?", (tid,))
            r = cur.fetchone()
            keygen_flag = int(r[0] or 0) if r else 0
            keygen_label = "🔒 Revoke Key Generation" if keygen_flag else "🔑 Allow Key Generation"
            kb = [
                [InlineKeyboardButton(keygen_label, callback_data=f"coadmin_keygen_toggle:{tid}")],
                [InlineKeyboardButton("⬇ Demote to UAdmin", callback_data=f"coadmin_demote:{tid}")],
                [InlineKeyboardButton("🚫 Revoke Access", callback_data=f"coadmin_revoke:{tid}")],
                [InlineKeyboardButton("☠️ Kick & Delete Data", callback_data=f"coadmin_kick_prompt:{tid}")],
                [InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]
            ]
            keygen_txt = "Allowed ✅" if keygen_flag else "Not Allowed ❌"
            await safe_edit(q, f"👤 Co-Admin ID: {tid}\nKey Generation: {keygen_txt}\nOwner can demote or remove co-owner", InlineKeyboardMarkup(kb))

        elif data.startswith("uadmin_view_buttons:"):
            tid = int(data.split(":")[1])
            if role == "co_admin":
                hidden_ids = await get_uadmin_hidden_id_set()
                if tid in hidden_ids:
                    await safe_edit(q, "Not found")
                    return
            cur = await db.aexecute("SELECT id, name, visibility, locked FROM buttons WHERE created_by =?", (tid,))
            rows = cur.fetchall()
            if not rows:
                await safe_edit(q, f"No folders by UAdmin {tid}", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"uadmin_view:{tid}")]]))
                return
            kb = [[InlineKeyboardButton(f"{display_button_name({'name': r[1], 'locked': r[3]})} [{r[2]}]", callback_data=f"manage_btn:{r[0]}")] for r in rows[:30]]
            kb.append([InlineKeyboardButton("Back", callback_data=f"uadmin_view:{tid}")])
            await safe_edit(q, f"Folders by UAdmin {tid} - Owner view (click to see data):", InlineKeyboardMarkup(kb))

        elif data.startswith("uadmin_set_nick:"):
            await set_user_state(uid, "awaiting_set_nickname", {"target_id": int(data.split(":")[1])})
            await safe_edit(q, f"✏ Send new nickname for ID {data.split(':')[1]}:")



        elif data.startswith("uadmin_promote:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM user_admins WHERE user_id =?", (tid,))
            await db.aexecute("INSERT OR REPLACE INTO co_admins (user_id, added_by, created_at) VALUES (?,?,?)", (tid, int(uid), datetime.now(timezone.utc).isoformat()))
            await refresh_cache(force=True)
            await safe_edit(q, f"✅ Promoted {tid} to Co-Admin", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_uadmins")]]))

        elif data.startswith("uadmin_hide_toggle:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            cur = await db.aexecute("SELECT hidden_from_coowner FROM user_admins WHERE user_id =?", (tid,))
            r = cur.fetchone()
            cur_flag = int(r[0] or 0) if r else 0
            new_flag = 0 if cur_flag else 1
            await db.aexecute("UPDATE user_admins SET hidden_from_coowner =? WHERE user_id =?", (new_flag, tid))
            await refresh_cache(force=True)
            status = "hidden from" if new_flag else "visible to"
            await safe_edit(q, f"✅ UAdmin {tid} is now {status} Co-Owners", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"uadmin_view:{tid}")]]))

        elif data.startswith("coadmin_keygen_toggle:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            cur = await db.aexecute("SELECT can_gen_keys FROM co_admins WHERE user_id =?", (tid,))
            r = cur.fetchone()
            cur_flag = int(r[0] or 0) if r else 0
            new_flag = 0 if cur_flag else 1
            await db.aexecute("UPDATE co_admins SET can_gen_keys =? WHERE user_id =?", (new_flag, tid))
            await refresh_cache(force=True)
            status = "allowed" if new_flag else "revoked"
            await safe_edit(q, f"✅ Key generation {status} for Co-Admin {tid}", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"coadmin_view:{tid}")]]))

        elif data.startswith("coadmin_demote:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM co_admins WHERE user_id =?", (tid,))
            await db.aexecute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)", (tid, f"UAdmin-{tid}", int(uid), datetime.now(timezone.utc).isoformat()))
            await refresh_cache(force=True)
            await safe_edit(q, f"✅ Demoted {tid} to UAdmin", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]]))

        elif data.startswith("coadmin_revoke:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM co_admins WHERE user_id =?", (tid,))
            await db.aexecute("DELETE FROM user_states WHERE user_id =?", (tid,))
            await refresh_cache(force=True)
            await safe_edit(q, f"✅ Co-Admin {tid} access revoked (data safe).", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]]))

        elif data.startswith("coadmin_kick_prompt:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            kb = [
                [InlineKeyboardButton("✅ Confirm Kick", callback_data=f"coadmin_kick_confirm:{tid}")],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"coadmin_view:{tid}")]
            ]
            await safe_edit(q, f"⚠ ARE YOU SURE?\nCo-Admin {tid} ka access aur saare folders delete ho jayenge permanently!", InlineKeyboardMarkup(kb))

        elif data.startswith("coadmin_kick_confirm:"):
            if not is_owner(uid): return
            tid = int(data.split(":")[1])
            await db.aexecute("DELETE FROM buttons WHERE created_by =?", (tid,))
            await db.aexecute("DELETE FROM co_admins WHERE user_id =?", (tid,))
            await db.aexecute("DELETE FROM user_states WHERE user_id =?", (tid,))
            invalidate_button_cache()
            await refresh_cache(force=True)
            await safe_edit(q, f"☠️ Co-Admin {tid} and their data completely deleted.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_list_coadmin")]]))

        
        elif data == "owner_shutdown":
            if not is_owner(uid): return
            await safe_edit(q, "♻ Bot shutting down safely... Render will auto-restart.")
            loop = asyncio.get_running_loop()
            loop.create_task(_graceful_shutdown_sequence())

        elif data == "rebuild_all_pdfs":
            if not is_owner(uid): return
            if not BACKUP_CHANNEL_ID:
                await safe_edit(q, "❌ BACKUP_CHANNEL_ID not set hai.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            cur = await db.aexecute("SELECT COUNT(*) FROM buttons")
            total = cur.fetchone()[0]
            await safe_edit(q, f"⚠️ Kya aap sure hain?\n\nYe {total} buttons ka PDF rebuild karega.\nIs mein time lag sakta hai.", InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Haan, Rebuild Karo", callback_data="rebuild_all_confirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]
            ]))

        elif data == "rebuild_all_confirm":
            if not is_owner(uid): return
            if not BACKUP_CHANNEL_ID:
                await safe_edit(q, "❌ BACKUP_CHANNEL_ID not set hai.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            cur = await db.aexecute("SELECT id, name, visibility, created_by, visible_to_user_id, locked, visible_to_user_ids FROM buttons ORDER BY id")
            rows = cur.fetchall()
            buttons = [
                {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4], "locked": r[5], "visible_to_user_ids": r[6]}
                for r in rows
            ]
            total = len(buttons)
            await safe_edit(q, f"🔄 Rebuild start: 0/{total} buttons...")
            done = 0
            success = 0
            failed = 0
            for btn in buttons:
                try:
                    await refresh_button_pdf_backup(context, int(btn["id"]), btn)
                    success += 1
                except Exception:
                    failed += 1
                done += 1
                if done % 3 == 0 or done == total:
                    try: await q.message.edit_text(f"🔄 Rebuilding... {done}/{total} done\n✅ {success} success | ❌ {failed} failed")
                    except Exception: pass
                await asyncio.sleep(0.2)
            await q.message.edit_text(f"✅ Rebuild Complete!\n\n📊 Total: {total}\n✅ Success: {success}\n❌ Failed: {failed}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

        elif data == "rebuild_single_prompt":
            if not is_owner(uid): return
            if not BACKUP_CHANNEL_ID:
                await safe_edit(q, "❌ BACKUP_CHANNEL_ID not set hai.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            cur = await db.aexecute("SELECT id, name FROM buttons ORDER BY id")
            btn_rows = cur.fetchall()
            if not btn_rows:
                await safe_edit(q, "❌ Koi folder nahi mila.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
                return
            kb = [[InlineKeyboardButton(f"📄 {r[1]} (ID:{r[0]})", callback_data=f"rebuild_pick:{r[0]}")] for r in btn_rows[:30]]
            if len(btn_rows) > 30:
                kb.append([InlineKeyboardButton(f"... aur {len(btn_rows)-30} folders", callback_data="rebuild_single_prompt")])
            kb.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")])
            await safe_edit(q, "🔄 Kaunsa folder rebuild karna hai? Tap karo:", InlineKeyboardMarkup(kb))

        elif data.startswith("rebuild_pick:"):
            if not is_owner(uid): return
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not btn:
                await safe_edit(q, "❌ Folder nahi mila.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
                return
            await safe_edit(q, f"🔄 Rebuilding PDF for: {btn.get('name')}...")
            try:
                await refresh_button_pdf_backup(context, bid, btn)
                await q.message.edit_text(f"✅ PDF rebuild done for: {btn.get('name')}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Rebuild Another", callback_data="rebuild_single_prompt"), InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))
            except Exception as e:
                await q.message.edit_text(f"❌ Rebuild failed for: {btn.get('name')}\nError: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data=f"rebuild_pick:{bid}"), InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))


        elif data.startswith("manage_btn:"):
            bid = int(data.split(":")[1])
            await show_manage_button_menu(update, context, bid, role, "admin_manage_list")

        elif data.startswith("m_lock_toggle:"):
            if role != "owner": return
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not btn: return
            new_locked = 0 if is_locked_button(btn) else 1
            await db.aexecute("UPDATE buttons SET locked =? WHERE id =?", (new_locked, bid))
            invalidate_button_cache()
            btn["locked"] = new_locked
            status = "locked" if new_locked else "unlocked"
            await safe_edit(q, f"Folder {display_admin_button_name(btn)} {status}", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

        elif data.startswith("m_addfile:"):
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_add_files_to_button(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return

            btn_name = btn.get('name') if btn and btn.get('name') else f"ID {bid}"
            text = f"📤 Send files in {btn_name}\n\nFiles bhejo, ho jaye to Done dabana."
            kb = [[InlineKeyboardButton("❌ Cancel", callback_data="m_cancel_upload")]]

            PENDING_UPLOAD_STATUS[uid] = {"mid": None, "count": 0, "chat_id": q.message.chat_id}

            await set_user_state(uid, "awaiting_file_upload", {"button_id": bid, "upload_msg_ids": [q.message.message_id]})
            await safe_edit(q, text, InlineKeyboardMarkup(kb))

        elif data == "m_cancel_upload":
            cancel_pending_upload_confirm(uid)

            if uid in PENDING_UPLOAD_STATUS:
                st_info = PENDING_UPLOAD_STATUS.pop(uid, None)
                if st_info and st_info.get('mid'):
                    try: await context.bot.delete_message(chat_id=st_info['chat_id'], message_id=st_info['mid'])
                    except Exception: pass
            stop_chat_action_loop(uid)

            st = await get_user_state(uid)
            upload_ids = st['data'].get('upload_msg_ids', []) if st else []
            for mid in upload_ids: schedule_delete_30(context.bot, q.message.chat_id, mid)
            schedule_delete_30(context.bot, q.message.chat_id, q.message.message_id)

            await clear_user_state(uid)
            m = await q.message.reply_text("❌ Cancelled. 30 sec me delete...")
            schedule_delete_30(context.bot, q.message.chat_id, m.message_id)
            try: await q.delete_message()
            except Exception as e: pass

        elif data == "m_done_upload":
            cancel_pending_upload_confirm(uid)

            if uid in PENDING_UPLOAD_STATUS:
                st_info = PENDING_UPLOAD_STATUS.pop(uid, None)
                if st_info and st_info.get('mid'):
                    try: await context.bot.delete_message(chat_id=st_info['chat_id'], message_id=st_info['mid'])
                    except Exception: pass
            stop_chat_action_loop(uid)

            st = await get_user_state(uid)
            upload_ids = st['data'].get('upload_msg_ids', []) if st else []
            for mid in upload_ids: schedule_delete_30(context.bot, q.message.chat_id, mid)
            schedule_delete_30(context.bot, q.message.chat_id, q.message.message_id)
            await clear_user_state(uid)
            m = await q.message.reply_text("✅ Upload done. 30 sec me delete...")
            schedule_delete_30(context.bot, q.message.chat_id, m.message_id)
            try: await q.delete_message()
            except Exception as e: pass

        elif data.startswith("m_listfiles:"):
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_edit_button(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            cur = await db.aexecute("SELECT id, file_type FROM button_files WHERE button_id =?", (bid,))
            rows = cur.fetchall()
            if not rows:
                await safe_edit(q, "No files", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
                return
            kb = [[InlineKeyboardButton(f"🗑 {r[1]} {r[0]}", callback_data=f"m_delfile:{r[0]}:{bid}")] for r in rows[:20]]
            kb.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
            await safe_edit(q, f"Files for {bid}:", InlineKeyboardMarkup(kb))

        elif data.startswith("m_delfile:"):
            _, fid, bid = data.split(":")
            btn = await get_button_by_id(bid)
            if not await can_edit_button(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            cur = await db.aexecute("SELECT file_type FROM button_files WHERE id =?", (int(fid),))
            deleted_row = cur.fetchone()
            await db.aexecute("DELETE FROM button_files WHERE id =?", (int(fid),))
            if deleted_row and deleted_row[0] in PDF_MERGE_TYPES:
                await refresh_button_pdf_backup(context, bid, btn)
            await safe_edit(q, "Deleted", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

        elif data.startswith("m_make_public:"):
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_uadmin_make_public(uid, btn, role):
                await safe_edit(q, "❌ Not allowed")
                return
            await db.aexecute("UPDATE buttons SET visibility = 'all', visible_to_user_id = NULL, visible_to_user_ids = NULL WHERE id =?", (bid,))
            invalidate_button_cache()
            await safe_edit(q, f"✅ Folder is now Public! Sab uadmins dekh sakte hai.", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

        elif data.startswith("m_delbtn:"):
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_edit_button(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            await delete_button_pdf_backup(context, bid)
            await db.aexecute("DELETE FROM buttons WHERE id =?", (bid,))
            invalidate_button_cache()
            await safe_edit(q, "✅ Deleted")

        elif data.startswith("m_vis:"):
            if role == "user_admin":
                await safe_edit(q, "❌ UAdmin sirf 'Make Public' button use karo")
                return
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_change_visibility(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            rows = [[InlineKeyboardButton(name, callback_data=f"m_vis_set:{bid}:{val}")] for name, val in visible_vis_options_for_role(role)]
            rows.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
            await safe_edit(q, f"Current Visibility: {format_visibility_mode(btn)}\nVisibility for {bid}: (Public = All)", InlineKeyboardMarkup(rows))

        elif data.startswith("m_vis_set:"):
            if role == "user_admin": return
            _, bid, vis = data.split(":")
            bid = int(bid)
            btn = await get_button_by_id(bid)
            if not await can_change_visibility(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            if vis == "specific_uadmin":
                uadmins = await get_all_user_admins()
                await set_user_state(uid, "awaiting_edit_vis_multi", {"button_id": bid, "selected": []})
                rows = render_uadmin_multiselect_kb(uadmins, set(), f"m_vis_multi_toggle:{bid}:", f"m_vis_multi_confirm:{bid}", f"m_vis:{bid}")
                await safe_edit(q, "Select UAdmin(s) - tap to toggle, then Confirm:", rows)
                return
            await db.aexecute("UPDATE buttons SET visibility =?, visible_to_user_id = NULL, visible_to_user_ids = NULL WHERE id =?", (vis, bid))
            invalidate_button_cache()
            await safe_edit(q, f"Vis -> {vis} (All = public)", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

        elif data.startswith("m_vis_specific:"):
            _, bid, target_id = data.split(":")
            btn = await get_button_by_id(bid)
            if not await can_change_visibility(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            await db.aexecute("UPDATE buttons SET visibility = 'specific_uadmin', visible_to_user_id =? WHERE id =?", (int(target_id), int(bid)))
            invalidate_button_cache()
            await safe_edit(q, f"Vis -> Specific UAdmin {target_id}", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

        elif data.startswith("m_vis_multi_toggle:"):
            _, bid, target_id = data.split(":")
            bid = int(bid); target_id = int(target_id)
            btn = await get_button_by_id(bid)
            if not await can_change_visibility(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            st = await get_user_state(uid)
            if not st or st['state']!= "awaiting_edit_vis_multi": return
            selected = set(st['data'].get('selected', []))
            if target_id in selected: selected.discard(target_id)
            else: selected.add(target_id)
            st['data']['selected'] = list(selected)
            await set_user_state(uid, "awaiting_edit_vis_multi", st['data'])
            uadmins = await get_all_user_admins()
            rows = render_uadmin_multiselect_kb(uadmins, selected, f"m_vis_multi_toggle:{bid}:", f"m_vis_multi_confirm:{bid}", f"m_vis:{bid}")
            await safe_edit(q, "Select UAdmin(s) - tap to toggle, then Confirm:", rows)

        elif data.startswith("m_vis_multi_confirm:"):
            bid = int(data.split(":")[1])
            btn = await get_button_by_id(bid)
            if not await can_change_visibility(uid, btn, role):
                await safe_edit(q, "Locked or not allowed")
                return
            st = await get_user_state(uid)
            selected = st['data'].get('selected', []) if st else []
            if not selected:
                await safe_edit(q, "⚠ Kam se kam 1 UAdmin select karo")
                return
            ids_csv = ",".join(str(x) for x in selected)
            first_id = selected[0]
            await db.aexecute(
                "UPDATE buttons SET visibility = 'specific_uadmin', visible_to_user_id =?, visible_to_user_ids =? WHERE id =?",
                (int(first_id), ids_csv, bid)
            )
            invalidate_button_cache()
            await clear_user_state(uid)
            await safe_edit(q, f"Vis -> Specific UAdmin(s): {len(selected)} selected", InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

    finally:
        pass


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()

    state_obj = await get_user_state(uid)
    state = state_obj['state'] if state_obj else None
    sdata = state_obj['data'] if state_obj else {}

    if state == "awaiting_access_key":
        key_input = text.upper().strip()
        cur = await db.aexecute("SELECT key, nickname, key_type FROM access_keys WHERE key =? AND is_used = 0", (key_input,))
        r = cur.fetchone()
        if r:
            is_uadmin_key = key_input.startswith("UADMIN-") or r[2] == 'uadmin'
            await db.aexecute("UPDATE access_keys SET is_used = 1, used_by =? WHERE key =?", (int(uid), key_input))
            if is_uadmin_key:
                await db.aexecute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)", (int(uid), r[1] or f"UAdmin-{uid}", int(OWNER_ID), datetime.now(timezone.utc).isoformat()))
                await refresh_cache(force=True)
                await clear_user_state(uid)
                await update.effective_message.reply_text(f"✅ UAdmin Granted Nick:{r[1]}")
            else:
                await clear_user_state(uid)
                await update.effective_message.reply_text("✅ Access granted (UAdmin fallback)!")
                await db.aexecute("INSERT OR REPLACE INTO user_admins (user_id, nickname, created_by, created_at) VALUES (?,?,?,?)", (int(uid), f"UAdmin-{uid}", int(OWNER_ID), datetime.now(timezone.utc).isoformat()))
                await refresh_cache(force=True)
            await show_main_menu(update, context, 0)
        else:
            await update.effective_message.reply_text("❌ Invalid key")
        return

    if not await is_authorized(uid):
        await update.effective_message.reply_text("❌ Access denied. Contact admin.")
        return

    if await dark_mode_controller.handle_text_state(update, context, state, sdata): return

    if state == "awaiting_uadmin_nickname":
        await db.aexecute("INSERT INTO access_keys (key, is_used, nickname, key_type, created_at, generated_by) VALUES (?, 0,?, 'uadmin',?,?)", (sdata.get('key'), text, datetime.now(timezone.utc).isoformat(), int(uid)))
        await clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ UAdmin Key `{sdata.get('key')}` Nick:{text}", parse_mode="Markdown")
        return

    if state == "awaiting_set_nickname":
        await db.aexecute("UPDATE user_admins SET nickname =? WHERE user_id =?", (text, sdata.get('target_id')))
        await refresh_cache(force=True)
        await clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ Nick -> {text}")
        return

    if state == "awaiting_new_button_name":
        if not text:
            await update.effective_message.reply_text("Valid name bhejo")
            return
        role = await get_user_role(uid)
        if role == "user_admin":
            try:
                await db.aexecute("INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback',?,?)", (text, int(uid), int(uid)))
                invalidate_button_cache()
                await update.effective_message.reply_text(f"✅ Folder '{text}' created in your private partition!")
            except Exception as e:
                await update.effective_message.reply_text(f"❌ Exists: {e}")
            await clear_user_state(uid)
            await show_main_menu(update, context, 0)
            return
        else:
            await set_user_state(uid, "awaiting_new_button_vis", {"name": text})
            rows = [[InlineKeyboardButton(name, callback_data=f"vis_{val}")] for name, val in visible_vis_options_for_role(role)]
            await update.effective_message.reply_text("👁 Visibility choose karo:", reply_markup=InlineKeyboardMarkup(rows))
            return

    if state == "awaiting_coadmin_id":
        try:
            nid = int(re.search(r'\d+', text).group())
            await db.aexecute("INSERT OR REPLACE INTO co_admins (user_id, added_by, created_at) VALUES (?,?,?)", (nid, int(uid), datetime.now(timezone.utc).isoformat()))
            await refresh_cache(force=True)
            await update.effective_message.reply_text(f"✅ Co-Admin {nid} added")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")
        await clear_user_state(uid)
        return

    if state == "awaiting_file_upload":
        bid = sdata.get('button_id')
        btn = await get_button_by_id(bid)
        role = await get_user_role(uid)
        if not await can_add_files_to_button(uid, btn, role):
            await clear_user_state(uid)
            await update.effective_message.reply_text("Locked or not allowed")
            return
        upload_ids = sdata.get('upload_msg_ids', [])
        if text == "✅ Done":
            cancel_pending_upload_confirm(uid)
            
            if uid in PENDING_UPLOAD_STATUS:
                st_info = PENDING_UPLOAD_STATUS.pop(uid, None)
                if st_info and st_info.get('mid'):
                    try: await context.bot.delete_message(chat_id=st_info['chat_id'], message_id=st_info['mid'])
                    except Exception: pass
            stop_chat_action_loop(uid)
            
            for mid in upload_ids: schedule_delete_30(context.bot, update.effective_chat.id, mid)
            await clear_user_state(uid)
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
            doc_type = "pdf" if is_pdf_upload(msg) else "document"
            file_info = {"file_id": msg.document.file_id, "file_unique_id": msg.document.file_unique_id, "file_type": doc_type, "caption": msg.caption or ""}
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
            chat_id = update.effective_chat.id
            if PENDING_UPLOAD_STATUS.get(uid) and PENDING_UPLOAD_STATUS[uid].get('mid') is None:
                status_msg = await context.bot.send_message(chat_id, "⏳ Uploading... 1 file received... Please wait ⏳")
                PENDING_UPLOAD_STATUS[uid] = {"mid": status_msg.message_id, "count": 1, "chat_id": chat_id}
                upload_ids.append(status_msg.message_id)
                sdata['upload_msg_ids'] = upload_ids
                await set_user_state(uid, "awaiting_file_upload", sdata)
                await start_chat_action_loop(context, chat_id, uid)
            elif uid in PENDING_UPLOAD_STATUS:
                cur = PENDING_UPLOAD_STATUS[uid]['count'] + 1
                await update_uploading_status(context, uid, chat_id, cur)
                
            backup_chat = None
            backup_mid = None
            if BACKUP_CHANNEL_ID and file_info['file_type']!= 'text':
                try:
                    backup_caption = backup_caption_with_button(file_info['caption'], btn.get('name') if btn else "")
                    if file_info['file_type'] == 'photo': bm = await context.bot.send_photo(int(BACKUP_CHANNEL_ID), photo=file_info['file_id'], caption=backup_caption)
                    elif file_info['file_type'] == 'video': bm = await context.bot.send_video(int(BACKUP_CHANNEL_ID), video=file_info['file_id'], caption=backup_caption)
                    elif file_info['file_type'] == 'audio': bm = await context.bot.send_audio(int(BACKUP_CHANNEL_ID), audio=file_info['file_id'], caption=backup_caption)
                    elif file_info['file_type'] == 'voice': bm = await context.bot.send_voice(int(BACKUP_CHANNEL_ID), voice=file_info['file_id'], caption=backup_caption)
                    elif file_info['file_type'] in ('document', 'pdf'): bm = await context.bot.send_document(int(BACKUP_CHANNEL_ID), document=file_info['file_id'], caption=backup_caption)
                    else: bm = await context.bot.copy_message(chat_id=int(BACKUP_CHANNEL_ID), from_chat_id=update.effective_chat.id, message_id=msg.message_id)
                    backup_chat = int(BACKUP_CHANNEL_ID)
                    backup_mid = bm.message_id
                except Exception as be:
                    logging.error(f"backup fail {be}")

            await db.aexecute(
                "INSERT INTO button_files (button_id, file_id, file_unique_id, file_type, caption, backup_chat_id, backup_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (bid, file_info['file_id'], file_info['file_unique_id'], file_info['file_type'], file_info['caption'], backup_chat, backup_mid, datetime.now(timezone.utc).isoformat())
            )

            sdata['upload_msg_ids'] = upload_ids
            await set_user_state(uid, "awaiting_file_upload", sdata)
            schedule_upload_confirm(context, uid, update.effective_chat.id, file_info['file_type'], bool(backup_mid))
            if file_info['file_type'] in PDF_MERGE_TYPES:
                schedule_pdf_rebuild(context, bid, btn)
        return

    if text:
        try:
            all_btns = await get_all_buttons_cached()
            text_lower = text.lower().strip()
            clean_text_lower = clean_button_text(text).lower()
            matched = BUTTON_CACHE.get("name_map", {}).get(text_lower) or BUTTON_CACHE.get("name_map", {}).get(clean_text_lower)
            role = await get_user_role(uid)
            if matched and await can_access_button_async(uid, matched, role):
                await send_button_files(update, context, matched)
        except Exception as e:
            logging.error(f"Text matching error: {e}")

async def dark_mode_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state_obj = await get_user_state(uid)
    state = state_obj['state'] if state_obj else None
    sdata = state_obj['data'] if state_obj else {}
    if await dark_mode_controller.handle_text_state(update, context, state, sdata):
        return

    if not await dark_mode_controller.is_enabled():
        try: await context.bot.delete_message(update.effective_chat.id, update.effective_message.message_id)
        except Exception as e: pass
        notice = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<b>Dark Mode is OFF</b>\n\nTry after sometime or contact owner.",
            parse_mode="HTML",
        )
        schedule_delete(context.bot, update.effective_chat.id, notice.message_id)
        return

    handled = await dark_mode_controller.handle_command(update, context)
    if handled: return

async def rebuildpdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    role = await get_user_role(uid)
    if not BACKUP_CHANNEL_ID:
        await update.effective_message.reply_text("BACKUP_CHANNEL_ID not set hai.")
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Use: /rebuildpdf <button_id>\nOwner: /rebuildpdf all")
        return

    if args[0].lower() == "all":
        if not is_owner(uid):
            await update.effective_message.reply_text("Sirf owner /rebuildpdf all chala sakta hai.")
            return
        cur = await db.aexecute("SELECT id, name, visibility, created_by, visible_to_user_id, locked, visible_to_user_ids FROM buttons ORDER BY id")
        rows = cur.fetchall()
        buttons = [
            {"id": r[0], "name": r[1], "visibility": r[2], "created_by": r[3], "visible_to_user_id": r[4], "locked": r[5], "visible_to_user_ids": r[6]}
            for r in rows
        ]
        status = await update.effective_message.reply_text(f"Rebuild start: {len(buttons)} buttons")
        done = 0
        for btn in buttons:
            await refresh_button_pdf_backup(context, int(btn["id"]), btn)
            done += 1
            await asyncio.sleep(0.2)
        await status.edit_text(f"Rebuild done: {done} buttons checked")
        return

    try: bid = int(args[0])
    except Exception as e:
        await update.effective_message.reply_text("Folder id number me bhejo. Example: /rebuildpdf 12")
        return
    btn = await get_button_by_id(bid)
    if not btn:
        await update.effective_message.reply_text("Folder nahi mila.")
        return
    if not await can_open_manage_button(uid, btn, role):
        await update.effective_message.reply_text("Is button ke liye permission nahi hai.")
        return
    await refresh_button_pdf_backup(context, bid, btn)
    await update.effective_message.reply_text(f"PDF rebuild checked for: {btn.get('name')}")

# ---------------- TELEGRAM APP SETUP ----------------
tg_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .connect_timeout(30)
    .read_timeout(30)
    .write_timeout(30)
    .pool_timeout(30)
    .build()
)
tg_app.add_handler(CommandHandler("start", start_handler))
tg_app.add_handler(CommandHandler("rebuildpdf", rebuildpdf_handler))
tg_app.add_handler(MessageHandler(filters.COMMAND, dark_mode_command_handler))
tg_app.add_handler(CallbackQueryHandler(callback_handler))
tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
telegram_started = False

# Optimization 2: RAM Watcher Loop
async def ram_watcher():
    while True:
        await asyncio.sleep(3600)
        try:
            import psutil, gc, os
            proc = psutil.Process(os.getpid())
            ram_mb = proc.memory_info().rss / 1024 / 1024
            if ram_mb > 450:
                BUTTON_CACHE["buttons"] = []
                BUTTON_CACHE["by_id"] = {}
                BUTTON_CACHE["name_map"] = {}
                BUTTON_CACHE["ts"] = 0
                CACHE["ts"] = 0
                gc.collect()
                logging.info(f"RAM Watcher cleaned, RAM was {ram_mb}MB")
        except Exception as e:
            logging.error(f"ram watcher error {e}")
            try: import gc; gc.collect()
            except: pass

async def start_telegram_with_retry():
    global telegram_started
    await init_db()
    delay = 5
    while True:
        try:
            await tg_app.initialize()
            await tg_app.start()
            await dark_mode_controller.start(tg_app.bot)
            telegram_started = True
            asyncio.create_task(ram_watcher())  # Start the RAM watcher properly
            logging.info("Telegram app initialized.")
            return
        except (TimedOut, NetworkError) as e:
            logging.error(f"Telegram startup timeout/network issue: {e}. Retrying in {delay}s...")
        except Exception as e:
            logging.error(f"Telegram startup error: {type(e).__name__}: {e}. Retrying in {delay}s...")
        try: await tg_app.shutdown()
        except Exception as e: pass
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ---------------- aiohttp ROUTES ----------------
async def web_home(request):
    return web.Response(text="Bot Running - Final 950+ Lines - Fixed All Bugs - Shutdown Added")

async def web_keep_alive(request):
    try:
        await db.aexecute("SELECT 1")
        return web.json_response({"status": "ok", "msg": "Turso pinged"}, status=200)
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)

async def shutdown_route(request):
    if request.query.get("owner") == OWNER_ID:
        loop.create_task(_graceful_shutdown_sequence())
        return web.json_response({"status": "shutting down"}, status=200)
    return web.json_response({"error": "unauthorized"}, status=403)

async def start_web_server():
    app = web.Application()
    app.add_routes([
        web.get('/', web_home),
        web.get('/keep-alive', web_keep_alive),
        web.get('/shutdown', shutdown_route),
        web.post('/shutdown', shutdown_route)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 5000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"aiohttp web server running on 0.0.0.0:{port}")
    return runner

async def _graceful_shutdown_sequence():
    logging.info("Shutting down gracefully...")
    try:
        if telegram_started:
            await dark_mode_controller.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await db.close()
    except Exception as e:
        logging.error(f"Graceful shutdown error: {e}")
    finally:
        loop.stop()

def handle_sigterm(signum, frame):
    logging.info("SIGTERM/SIGINT received, shutting down gracefully...")
    try:
        loop.call_soon_threadsafe(lambda: loop.create_task(_graceful_shutdown_sequence()))
    except Exception as e:
        logging.error(f"SIGTERM scheduling failed, forcing exit: {e}")
        sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

# ---------------- POLLING RUNNER ----------------
if __name__ == "__main__":
    async def start_polling():
        delay = 5
        while True:
            try:
                await tg_app.bot.delete_webhook(drop_pending_updates=True)
                logging.info("Webhook deleted, polling mode...")
                await tg_app.updater.start_polling(drop_pending_updates=True)
                logging.info(f"Polling started! Owner {OWNER_ID} Backup: {BACKUP_CHANNEL_ID}")
                return
            except (TimedOut, NetworkError) as e:
                logging.error(f"Polling startup timeout/network issue: {e}. Retrying in {delay}s...")
            except Exception as e:
                logging.error(f"Polling startup error: {type(e).__name__}: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    loop.run_until_complete(start_web_server())
    loop.run_until_complete(start_telegram_with_retry())
    loop.run_until_complete(start_polling())
    loop.run_forever()
