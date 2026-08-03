# =============================================================================
# app_4 - 24/7 - TELETHON LOGIC UNTOUCHED - TURSO HTTP PIPELINE (app_5 pattern)
# =============================================================================
import os, re, time, random, string, asyncio, logging, sqlite3, threading
from datetime import datetime
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import httpx

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("BotA")
for _n in ["telethon", "telegram", "httpx", "aiohttp"]:
    logging.getLogger(_n).setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
GROUP_ID = os.getenv("GROUP_ID")
BOT_B_USERNAME = os.getenv("BOT_B_USERNAME")
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit())
PORT = int(os.getenv("PORT", 10000))
TURSO_URL = (os.getenv("TURSO_DATABASE_URL") or "").strip()
TURSO_TOKEN = (os.getenv("TURSO_AUTH_TOKEN") or "").strip()

telethon_lock = asyncio.Lock()
db_lock = asyncio.Lock()
waiting = {}
last_edit = {}
user_states = {}
admin_states = {}
telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
ptb_app = None
target_entity = None
bot_b_id = None
turso_client = None
db_mode = "unknown"
GROUP_ID_INT = int(GROUP_ID) if GROUP_ID and GROUP_ID.lstrip("-").isdigit() else None

# ---------- TURSO HTTP CLIENT - FROM app_5.py - NO STREAM, NO BATON ----------
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
        self.headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
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
        payload = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": self._args(params)}}, {"type": "close"}]}
        # 2 try - agar ek baar network hila toh turant retry
        for attempt in range(2):
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
                                t = col.get("type"); v = col.get("value")
                                if t == "integer":
                                    try: parsed.append(int(v))
                                    except: parsed.append(v)
                                elif t == "float":
                                    parsed.append(float(v))
                                elif t == "null":
                                    parsed.append(None)
                                else:
                                    parsed.append(v)
                            rows.append(tuple(parsed))
                return TursoCursor(rows)
            except Exception as e:
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                raise e
    async def close(self):
        if self.client:
            try: await self.client.aclose()
            except: pass

class CompatWrapper:
    """Purane code ko bina change kiye naya Turso chalane ke liye wrapper"""
    def __init__(self, turso_db):
        self.db = turso_db
    async def execute(self, q, p=()):
        cur = await self.db.aexecute(q, p)
        class RS:
            def __init__(self, r): self.rows = r
        return RS(cur.fetchall())

class LocalWrapper:
    def __init__(self, conn):
        self.c = conn
        self._thread_lock = threading.Lock()
    async def execute(self, q, p=()):
        def _do(conn, qq, pp):
            with self._thread_lock:
                cur = conn.cursor()
                cur.execute(qq, pp)
                try: rows = cur.fetchall()
                except: rows = []
                try: conn.commit()
                except: pass
                return rows
        rows = await asyncio.to_thread(_do, self.c, q, p)
        class RS:
            def __init__(self, r): self.rows = r
        return RS(rows)

async def get_turso():
    global turso_client, db_mode
    if turso_client is not None:
        return turso_client
    if TURSO_URL and TURSO_TOKEN:
        try:
            tdb = TursoDB(TURSO_URL, TURSO_TOKEN)
            # test ping
            await tdb.aexecute("SELECT 1")
            turso_client = CompatWrapper(tdb)
            db_mode = "TURSO-HTTP"
            log.info("Turso HTTP Connected ✅ - No Stream, No Down")
            return turso_client
        except Exception as e:
            log.error(f"Turso fail {e}, fallback local")
    db_path = os.getenv("LOCAL_DB_PATH", "bot_data.db")
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    turso_client = LocalWrapper(conn)
    db_mode = f"LOCAL:{db_path}"
    return turso_client

async def _reset_turso_client():
    global turso_client
    old = turso_client
    turso_client = None
    try:
        if old and hasattr(old, 'db'):
            await old.db.close()
    except: pass

async def db_init():
    client = await get_turso()
    await client.execute("""CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY, expiry REAL, created_at REAL, created_by INTEGER,
            is_used INTEGER DEFAULT 0, used_by INTEGER, used_at REAL,
            is_revoked INTEGER DEFAULT 0, revoked_at REAL)""")
    await client.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, key_used TEXT, authorized_at REAL,
            access_expiry REAL, is_banned INTEGER DEFAULT 0)""")
    try:
        await client.execute("UPDATE keys SET is_used=0 WHERE is_used IS NULL")
        await client.execute("UPDATE keys SET is_revoked=0 WHERE is_revoked IS NULL")
    except: pass

def gen_key(): return "BOT-" + "-".join(''.join(random.choices(string.ascii_uppercase+string.digits, k=4)) for _ in range(3))
def is_interim(t): return any(k in t.lower() for k in ["processing","fetching","working","generating","please wait","searching","checking"]) if t else False
def main_menu_kb(a=False):
    kb=[[InlineKeyboardButton("🔍 Search Number", callback_data="search")]]
    if a: kb.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)
def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Generate Keys", callback_data="gen_keys")],
        [InlineKeyboardButton("✅ Active Keys", callback_data="list_keys")],
        [InlineKeyboardButton("👥 Used Keys", callback_data="list_used")],
        [InlineKeyboardButton("🚫 Revoked Keys", callback_data="list_revoked")],
        [InlineKeyboardButton("📊 Stats", callback_data="db_status")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ])
async def safe_edit(cid, mid, txt, rm=None, reply_markup=None):
    markup = reply_markup if reply_markup is not None else rm
    if time.time() - last_edit.get((cid,mid),0) < 1.2: await asyncio.sleep(0.5)
    try:
        await ptb_app.bot.edit_message_text(chat_id=cid, message_id=mid, text=txt[:4090], reply_markup=markup)
        last_edit[(cid,mid)]=time.time()
    except: pass

# ---------- BRIDGE - TELETHON LOGIC EXACT SAME AS ORIGINAL app_4.py - NO CHANGE ----------
@telethon_client.on(events.NewMessage)
async def on_new(event):
    try:
        if not target_entity or not bot_b_id: return
        if GROUP_ID_INT is not None:
            if event.chat_id != GROUP_ID_INT: return
        else:
            if event.chat_id != target_entity.id: return
        if event.sender_id != bot_b_id: return
        txt = event.message.text or ""
        for cmd_id, data in list(waiting.items()):
            if data['future'].done(): continue
            if data['botB_msg_id'] is None:
                data['botB_msg_id'] = event.id
                if is_interim(txt):
                    asyncio.create_task(safe_edit(data['botA_chat'], data['botA_msg'], f"⏳ {txt}\n\nWorking..."))
                else:
                    data['future'].set_result(txt)
                break
            else:
                if not is_interim(txt):
                    data['future'].set_result(txt)
                    break
    except Exception as e:
        log.error(f"on_new err: {e}")

@telethon_client.on(events.MessageEdited)
async def on_edit(event):
    try:
        if not target_entity or not bot_b_id: return
        if GROUP_ID_INT is not None:
            if event.chat_id != GROUP_ID_INT: return
        if event.sender_id != bot_b_id: return
        txt = event.message.text or ""
        for data in waiting.values():
            if data['botB_msg_id'] == event.id and not data['future'].done():
                if is_interim(txt):
                    asyncio.create_task(safe_edit(data['botA_chat'], data['botA_msg'], f"⏳ {txt}"))
                else:
                    data['future'].set_result(txt)
                break
    except Exception as e:
        log.error(f"on_edit err: {e}")

# ---------- KEY SYSTEM - LOGIC SAME, ONLY DB CLIENT CHANGED TO HTTP ----------
def _is_retryable(e):
    msg = str(e).lower()
    return any(k in msg for k in ["stream", "hrana", "baton", "closed", "timeout", "connection"])

async def is_authorized_db(uid:int):
    if uid in ADMIN_IDS: return True
    for attempt in range(2):
        try:
            async with db_lock:
                client = await get_turso()
                rs = await client.execute("SELECT key_used, access_expiry, is_banned FROM users WHERE user_id=?", (uid,))
                if not rs.rows: return False
                key_used, exp, banned = rs.rows[0]
                if banned and banned==1: return False
                if exp and exp < time.time():
                    await client.execute("DELETE FROM users WHERE user_id=?", (uid,))
                    return False
                if key_used:
                    rk = await client.execute("SELECT COALESCE(is_revoked,0) FROM keys WHERE key=?", (key_used,))
                    if rk.rows and rk.rows[0][0]==1:
                        await client.execute("DELETE FROM users WHERE user_id=?", (uid,))
                        return False
                return True
        except Exception as e:
            log.error(f"auth err {uid} attempt {attempt}: {e}")
            if attempt==0 and _is_retryable(e):
                await _reset_turso_client()
                continue
            return True # DB down pe verified ko rokna nahi - 24/7
    return False

async def redeem_key_db(key_str, uid):
    now=time.time()
    for attempt in range(2):
        try:
            async with db_lock:
                client = await get_turso()
                rs = await client.execute("SELECT expiry, created_at, COALESCE(is_used,0), COALESCE(is_revoked,0), used_by FROM keys WHERE key=?", (key_str,))
                if not rs.rows: return "INVALID"
                expiry, created_at, is_used, is_revoked, used_by = rs.rows[0]
                if is_revoked==1: return "BANNED"
                if expiry and expiry < now: return "EXPIRED"
                if is_used==1: return "USED"
                validity = (expiry - created_at) if (expiry and created_at) else 30*86400
                access_exp = now + validity
                await client.execute("UPDATE keys SET is_used=1, used_by=?, used_at=? WHERE key=?", (uid, now, key_str))
                await client.execute("INSERT OR REPLACE INTO users (user_id, key_used, authorized_at, access_expiry, is_banned) VALUES (?,?,?,?,0)", (uid, key_str, now, access_exp))
                return "OK"
        except Exception as e:
            if attempt==0 and _is_retryable(e):
                await _reset_turso_client()
                continue
            return f"DB_ERROR: {e}"
    return "DB_ERROR"

async def generate_keys_db(count, days, admin_id):
    now=time.time(); expiry=now+days*86400; keys=[]
    for attempt in range(2):
        try:
            async with db_lock:
                client = await get_turso()
                for _ in range(count):
                    k=gen_key()
                    await client.execute("INSERT INTO keys (key, expiry, created_at, created_by, is_used, is_revoked) VALUES (?,?,?,?,0,0)", (k, expiry, now, admin_id))
                    keys.append(k)
            return keys
        except Exception as e:
            if attempt==0 and _is_retryable(e):
                await _reset_turso_client()
                keys=[]
                continue
            raise

async def revoke_key_db(key_str):
    now=time.time()
    for attempt in range(2):
        try:
            async with db_lock:
                client = await get_turso()
                await client.execute("UPDATE keys SET is_revoked=1, revoked_at=? WHERE key=?", (now, key_str))
                rs = await client.execute("SELECT used_by FROM keys WHERE key=?", (key_str,))
                banned_uid = rs.rows[0][0] if rs.rows and rs.rows[0][0] else None
                if banned_uid:
                    await client.execute("DELETE FROM users WHERE user_id=?", (banned_uid,))
                return banned_uid
        except Exception as e:
            if attempt==0 and _is_retryable(e):
                await _reset_turso_client()
                continue
            raise

# ---------- HANDLERS - SAME ----------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    auth = await is_authorized_db(uid)
    if auth:
        user_states[uid]={"mode":"IDLE","last_req":0}
        await update.message.reply_text(f"Welcome {update.effective_user.first_name} 👋", reply_markup=main_menu_kb(uid in ADMIN_IDS))
    else:
        await update.message.reply_text("🔒 **Access Key Required**\n`BOT-XXXX-XXXX-XXXX` bhejo\n\n⚠️ Agar pehle key thi to Admin ne ban kar di hai. Nayi key lo.", parse_mode="Markdown")

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer(); uid=q.from_user.id; data=q.data
    try:
        if data=="search":
            if not await is_authorized_db(uid):
                await q.edit_message_text("❌ **Key Expired / Banned by Admin**\n\n🔑 Nayi key bhejo.\n/start karke key bhejo.", parse_mode="Markdown")
                user_states.pop(uid, None)
                return
            user_states[uid]={"mode":"SEARCH","last_req":time.time()}
            await q.edit_message_text("📱 **Search Mode ON**\n10 digit number bhejo:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
        elif data=="main_menu":
            user_states[uid]={"mode":"IDLE","last_req":0}
            await q.edit_message_text("Main Menu:", reply_markup=main_menu_kb(uid in ADMIN_IDS))
        elif data=="admin_panel":
            if uid not in ADMIN_IDS: return
            await q.edit_message_text(f"👑 **Admin Panel**\nDB: {db_mode}\nOne Key = One Time Use", reply_markup=admin_kb(), parse_mode="Markdown")
        elif data=="gen_keys":
            if uid not in ADMIN_IDS: return
            admin_states[uid]={"step":"await_count"}
            await q.edit_message_text("Kitne keys banane hain? 1-20 bhejo:")
        elif data=="db_status":
            if uid not in ADMIN_IDS: return
            client=await get_turso()
            r1=await client.execute("SELECT COUNT(*) FROM keys WHERE COALESCE(is_used,0)=0 AND COALESCE(is_revoked,0)=0 AND expiry>?", (time.time(),))
            active = r1.rows[0][0] if r1.rows else 0
            r2=await client.execute("SELECT COUNT(*) FROM keys WHERE COALESCE(is_used,0)=1", ())
            used = r2.rows[0][0] if r2.rows else 0
            r3=await client.execute("SELECT COUNT(*) FROM keys WHERE COALESCE(is_revoked,0)=1", ())
            revoked = r3.rows[0][0] if r3.rows else 0
            r4=await client.execute("SELECT COUNT(*) FROM users", ())
            users = r4.rows[0][0] if r4.rows else 0
            txt = f"📊 **Stats**\n\n✅ Active: {active}\n👥 Used: {used}\n🚫 Revoked: {revoked}\n👤 Users: {users}"
            await q.edit_message_text(txt, reply_markup=admin_kb(), parse_mode="Markdown")
        elif data=="list_keys":
            if uid not in ADMIN_IDS: return
            client=await get_turso()
            rs=await client.execute("SELECT key, expiry FROM keys WHERE COALESCE(is_revoked,0)=0 AND COALESCE(is_used,0)=0 AND expiry>? ORDER BY created_at DESC LIMIT 15", (time.time(),))
            rows=rs.rows
            if not rows:
                await q.edit_message_text("No Active Keys. Generate karo.", reply_markup=admin_kb())
                return
            txt="✅ **Active Keys (One Time Use)**\n\n"; kb=[]
            for k,exp in rows:
                dleft=int((exp-time.time())/86400)
                txt+=f"`{k}` - {dleft}d\n"
                kb.append([InlineKeyboardButton(f"🚫 Ban {k[-4:]}", callback_data=f"revoke_{k}")])
            kb.append([InlineKeyboardButton("⬅ Back", callback_data="admin_panel")])
            await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        elif data=="list_used":
            if uid not in ADMIN_IDS: return
            client=await get_turso()
            rs=await client.execute("SELECT key, used_by FROM keys WHERE COALESCE(is_used,0)=1 AND COALESCE(is_revoked,0)=0 ORDER BY used_at DESC LIMIT 15", ())
            rows=rs.rows
            if not rows:
                await q.edit_message_text("No Used Keys.", reply_markup=admin_kb())
                return
            txt="👥 **Used Keys**\n\n"; kb=[]
            for k,used_by in rows:
                txt+=f"`{k}` -> `{used_by}`\n"
                kb.append([InlineKeyboardButton(f"🚫 Ban {k[-4:]}", callback_data=f"banused_{k}")])
            kb.append([InlineKeyboardButton("⬅ Back", callback_data="admin_panel")])
            await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        elif data=="list_revoked":
            if uid not in ADMIN_IDS: return
            client=await get_turso()
            rs=await client.execute("SELECT key FROM keys WHERE COALESCE(is_revoked,0)=1 LIMIT 15", ())
            rows=rs.rows
            if not rows:
                await q.edit_message_text("No Revoked Keys.", reply_markup=admin_kb())
                return
            txt="🚫 **Revoked Keys**\n\n"
            for r in rows: txt+=f"`{r[0]}`\n"
            await q.edit_message_text(txt, parse_mode="Markdown", reply_markup=admin_kb())
        elif data.startswith("revoke_") or data.startswith("banused_"):
            if uid not in ADMIN_IDS: return
            key_to_ban = data.replace("revoke_","").replace("banused_","")
            banned_uid = await revoke_key_db(key_to_ban)
            if banned_uid:
                user_states.pop(banned_uid, None)
                for cid,d in list(waiting.items()):
                    if d['user_id']==banned_uid:
                        if not d['future'].done(): d['future'].cancel()
                        await safe_edit(d['botA_chat'], d['botA_msg'], "❌ **Key Banned by Admin**")
                        waiting.pop(cid,None)
                try:
                    await ptb_app.bot.send_message(banned_uid, "🚫 **Your Key BANNED by Admin**\nNayi key lo\n/start", parse_mode="Markdown")
                except: pass
            await q.edit_message_text(f"✅ Banned `{key_to_ban}`\nUser {banned_uid}", parse_mode="Markdown", reply_markup=admin_kb())
    except Exception as e:
        log.error(f"cb err {e}")
        try: await q.edit_message_text(f"Error: {e}", reply_markup=admin_kb())
        except: pass

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; txt=(update.message.text or "").strip()
    if time.time() - user_states.get(uid,{}).get("last_req",0) < 1.0: return
    user_states[uid]={**user_states.get(uid,{}), "last_req":time.time()}
    if uid in ADMIN_IDS and uid in admin_states:
        if admin_states[uid].get("step")=="await_count" and txt.isdigit() and 1<=int(txt)<=20:
            admin_states[uid]["count"]=int(txt); admin_states[uid]["step"]="await_expiry"
            await update.message.reply_text("Expiry din? 1-30 bhejo:")
            return
        if admin_states[uid].get("step")=="await_expiry" and txt.isdigit() and 1<=int(txt)<=30:
            try:
                keys=await generate_keys_db(admin_states[uid]["count"], int(txt), uid)
                del admin_states[uid]
                await update.message.reply_text("✅ **One Time Use Keys**\n\n"+"\n".join([f"`{k}`" for k in keys]), parse_mode="Markdown", reply_markup=admin_kb())
            except Exception as e:
                await update.message.reply_text(f"❌ DB Error: {e}")
            return
    if not await is_authorized_db(uid):
        if re.match(r"^BOT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$", txt):
            res=await redeem_key_db(txt, uid)
            if res=="OK": await update.message.reply_text("✅ **Access Granted!**", reply_markup=main_menu_kb(uid in ADMIN_IDS))
            elif res=="USED": await update.message.reply_text("❌ **Used - One Time Use**")
            elif res=="BANNED": await update.message.reply_text("🚫 **Banned key**")
            elif res=="EXPIRED": await update.message.reply_text("⏰ **Expired**")
            else: await update.message.reply_text(f"❌ {res}")
        else:
            if user_states.get(uid,{}).get("mode")!="SEARCH":
                await update.message.reply_text("🔒 **Key Banned/Expired - Nayi key bhejo**\n`BOT-XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return
    if re.fullmatch(r"\d{10}", txt):
        asyncio.create_task(process_number(update, txt))

async def process_number(update: Update, number: str):
    if not await is_authorized_db(update.effective_user.id):
        await update.message.reply_text("🚫 **Key Banned / Expired**\n/start")
        return
    chat_id=update.effective_chat.id; uid=update.effective_user.id
    status_msg=await update.message.reply_text(f"🔍 `{number}` ⏳", parse_mode="Markdown")
    async with telethon_lock:
        fut=asyncio.get_running_loop().create_future(); cmd_msg=None
        try:
            await asyncio.sleep(1.0)
            cmd_msg=await telethon_client.send_message(target_entity, f"/num {number}")
            waiting[cmd_msg.id]={"future":fut,"botB_msg_id":None,"botA_chat":chat_id,"botA_msg":status_msg.message_id,"user_id":uid}
            final=await asyncio.wait_for(fut, timeout=60)
            await safe_edit(chat_id, status_msg.message_id, f"✅ **{number}**\n\n{final}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Again", callback_data="search")]]))
        except asyncio.TimeoutError:
            await safe_edit(chat_id, status_msg.message_id, "⏰ Timeout")
        except Exception as e:
            log.error(f"process_number err")
            await safe_edit(chat_id, status_msg.message_id, f"Error")
        finally:
            if cmd_msg: waiting.pop(cmd_msg.id,None)
            try: await telethon_client.delete_messages(target_entity, cmd_msg)
            except: pass

async def web_handler(r): return web.Response(text="BotA Running 24/7 - HTTP Turso ✅")

async def main():
    global ptb_app, target_entity, bot_b_id
    app = web.Application()
    app.router.add_get("/", web_handler)
    app.router.add_get("/ping", web_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()
    log.info(f"Web on {PORT}")
    while True:
        try:
            await db_init()
            await telethon_client.start()
            target_entity=await telethon_client.get_entity(GROUP_ID_INT if GROUP_ID_INT else GROUP_ID)
            bot_b_id=(await telethon_client.get_entity(BOT_B_USERNAME)).id
            ptb_app=Application.builder().token(BOT_TOKEN).build()
            ptb_app.add_handler(CommandHandler("start", start_cmd))
            ptb_app.add_handler(CallbackQueryHandler(cb_handler))
            ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
            await ptb_app.initialize(); await ptb_app.start(); await ptb_app.updater.start_polling()
            log.info(f"BotA 24/7 Started - DB {db_mode} - Telethon untouched")
            await asyncio.Event().wait()
        except Exception as e:
            log.error(f"Main crash {e} - restart in 5s")
            await asyncio.sleep(5)
            try:
                if ptb_app: await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()
            except: pass
            try: await telethon_client.disconnect()
            except: pass
            await _reset_turso_client()
            continue

if __name__=="__main__": asyncio.run(main())
