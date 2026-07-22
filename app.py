# app.py - UADMIN PARTITION + NICKNAME + ADVANCED VISIBILITY - POLLING 24/7
import os, re, random, string, asyncio, json, uuid, threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()

from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

app = Flask(__name__)

def clean_button_text(text: str) -> str:
    if not text: return ""
    t = text.strip()
    t = re.sub(r'[^\w\s\-\_\.\(\)\[\]\{\}]+', '', t, flags=re.UNICODE).strip()
    return t

def is_owner(uid): return str(uid) == OWNER_ID

def is_co_admin(uid):
    try:
        r = supabase.table("co_admins").select("user_id").eq("user_id", int(uid)).execute()
        return len(r.data) > 0
    except: return False

def is_user_admin(uid):
    try:
        r = supabase.table("user_admins").select("user_id").eq("user_id", int(uid)).execute()
        return len(r.data) > 0
    except: return False

def get_all_user_admins():
    try:
        return supabase.table("user_admins").select("*").execute().data
    except: return []

def get_all_co_admin_ids():
    try:
        r = supabase.table("co_admins").select("user_id").execute()
        return [int(x['user_id']) for x in r.data]
    except: return []

def get_user_admin_ids():
    try:
        return [int(x['user_id']) for x in get_all_user_admins()]
    except: return []

def is_authorized(uid):
    if is_owner(uid): return True
    if is_co_admin(uid): return True
    if is_user_admin(uid): return True
    try:
        r = supabase.table("authorized_users").select("user_id").eq("user_id", int(uid)).execute()
        return len(r.data) > 0
    except: return False

def get_user_role(uid):
    if is_owner(uid): return "owner"
    if is_co_admin(uid): return "co_admin"
    if is_user_admin(uid): return "user_admin"
    if is_authorized(uid): return "normal_user"
    return "unauthorized"

def get_user_state(uid):
    try:
        r = supabase.table("user_states").select("*").eq("user_id", int(uid)).execute()
        return r.data[0] if r.data else None
    except: return None

def set_user_state(uid, state, data=None):
    if data is None: data = {}
    supabase.table("user_states").upsert({
        "user_id": int(uid), "state": state, "data": data,
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="user_id").execute()

def clear_user_state(uid):
    try: supabase.table("user_states").delete().eq("user_id", int(uid)).execute()
    except: pass

def generate_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"KEY-{rand}"

def generate_uadmin_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"UADMIN-{rand}"

async def auto_delete_message(bot, chat_id, message_id, delay=15):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

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
    if role == "owner": return True
    created_by = btn.get('created_by')
    vis = btn.get('visibility','all')
    visible_to = btn.get('visible_to_user_id')

    # apna banaya hua button hamesha dikhega
    if created_by and int(created_by) == int(uid):
        return True

    if role == "co_admin":
        if created_by and int(created_by) in user_admin_ids:
            return False # user admin ka button main menu me nahi, sirf User Admin List se
        if vis == "owner_only": return False
        if vis.startswith("specific_uadmin"): return True # co-admin ko dikhega manage ke liye
        return True

    if role == "user_admin":
        if created_by and int(created_by) in user_admin_ids and int(created_by)!= int(uid):
            return False # dusre uadmin ka private button nahi
        if vis == "all": return True
        if vis == "uadmins_only": return True
        if vis.startswith("specific_uadmin"):
            if visible_to and int(visible_to) == int(uid): return True
            return False
        return False

    if role == "normal_user":
        if created_by and int(created_by) in user_admin_ids:
            return False
        if vis in ("all", "users_owner_only"): return True
        return False
    return False

def get_buttons_paginated_for_user(uid, page):
    try:
        all_btns = supabase.table("buttons").select("*").order("name").execute().data
    except Exception as e:
        print("get_buttons error", e)
        return [], 0
    role = get_user_role(uid)
    user_admin_ids = get_user_admin_ids()
    filtered = [b for b in all_btns if can_view_button(uid, b, role, user_admin_ids)]
    total = len(filtered)
    start = page*PER_PAGE
    return filtered[start:start+PER_PAGE], total

def get_manage_buttons_for_user(uid):
    try:
        all_btns = supabase.table("buttons").select("*").order("name").execute().data
    except: return []
    role = get_user_role(uid)
    user_admin_ids = get_user_admin_ids()
    if role == "owner": return all_btns
    if role == "co_admin":
        return [b for b in all_btns if not (b.get('created_by') and int(b.get('created_by')) in user_admin_ids)]
    if role == "user_admin":
        return [b for b in all_btns if b.get('created_by') and int(b.get('created_by')) == int(uid)]
    return []

async def send_button_files(update, context, button):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    role = get_user_role(uid)
    if not can_view_button(uid, button, role, get_user_admin_ids()):
        msg = await context.bot.send_message(chat_id, "❌ You can't view this button")
        schedule_delete(context.bot, chat_id, msg.message_id)
        return
    try:
        files_res = supabase.table("button_files").select("*").eq("button_id", button['id']).execute()
        files = files_res.data
        if not files:
            msg = await context.bot.send_message(chat_id, f"📭 '{button['name']}' is empty.")
            schedule_delete(context.bot, chat_id, msg.message_id)
            return
        for f in files:
            try:
                caption = (f.get('caption') or "") + "\n\n⏳ Auto-delete 15 sec..."
                ftype = f.get('file_type','document'); fid = f.get('file_id')
                if ftype == 'text' or not fid or fid.startswith('text_'):
                    m = await context.bot.send_message(chat_id, text=f.get('caption') or "No content")
                elif ftype == 'photo': m = await context.bot.send_photo(chat_id, photo=fid, caption=caption)
                elif ftype == 'video': m = await context.bot.send_video(chat_id, video=fid, caption=caption)
                elif ftype == 'audio': m = await context.bot.send_audio(chat_id, audio=fid, caption=caption)
                elif ftype == 'voice': m = await context.bot.send_voice(chat_id, voice=fid, caption=caption)
                elif ftype == 'video_note': m = await context.bot.send_video_note(chat_id, video_note=fid)
                elif ftype == 'sticker': m = await context.bot.send_sticker(chat_id, sticker=fid)
                else: m = await context.bot.send_document(chat_id, document=fid, caption=caption)
                schedule_delete(context.bot, chat_id, m.message_id)
            except Exception as e:
                await context.bot.send_message(chat_id, f"⚠ Error: {e}")
    except Exception as e:
        await context.bot.send_message(chat_id, f"Error: {e}")

async def show_main_menu(update, context, page=0):
    uid = update.effective_user.id
    buttons, total = get_buttons_paginated_for_user(uid, page)
    total_pages = max(1, (total + PER_PAGE - 1)//PER_PAGE)
    inline_rows = []; r=[]
    for b in buttons:
        r.append(build_inline_button(b))
        if len(r)==2: inline_rows.append(r); r=[]
    if r: inline_rows.append(r)
    pag_row=[]
    if page>0: pag_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"main_page:{page-1}"))
    if page < total_pages-1: pag_row.append(InlineKeyboardButton("Next ➡", callback_data=f"main_page:{page+1}"))
    if pag_row: inline_rows.append(pag_row)
    if is_owner(uid) or is_co_admin(uid) or is_user_admin(uid):
        inline_rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    text = f"📂 Main Menu (Page {page+1}/{total_pages}) - {total} buttons"
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
            await update.callback_query.answer()
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
    except:
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
        await update.effective_message.reply_text("❌ Admin only"); return
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
        await update.effective_message.reply_text("🔐 Welcome! Send Access Key.\nKEY-XXXX for normal user\nUADMIN-XXXX for User Admin")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    role = get_user_role(uid)

    if data.startswith("view_btn:"):
        _, bid, page = data.split(":")
        res = supabase.table("buttons").select("*").eq("id", int(bid)).execute()
        if not res.data: await q.answer("Not found"); return
        btn = res.data[0]
        if not can_view_button(uid, btn, role, get_user_admin_ids()):
            await q.answer("❌ No access"); return
        await q.answer()
        await send_button_files(update, context, btn)

    elif data.startswith("main_page:"):
        page = int(data.split(":")[1])
        await show_main_menu(update, context, page)

    # ---- NEW VISIBILITY FOR NEW BUTTON ----
    elif data.startswith("vis_"):
        st = get_user_state(uid)
        if not st or st['state']!="awaiting_new_button_vis": await q.answer(); return
        sdata = st['data']
        vis = data.replace("vis_","")
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("❌ No UAdmins found. First create UAdmin.")
                return
            rows=[]
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"vis_specific_select:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👤 Select UAdmin for Specific:", reply_markup=InlineKeyboardMarkup(rows))
            await q.answer(); return
        # normal create
        try:
            ins = {"name": sdata['name'], "visibility": vis, "btn_type": "callback", "color": "", "emoji": "", "created_by": int(uid), "visible_to_user_id": None}
            supabase.table("buttons").insert(ins).execute()
            await q.edit_message_text(f"✅ Button '{sdata['name']}' created! Vis: {vis}")
        except Exception as e:
            await q.edit_message_text(f"Error: {e}")
        clear_user_state(uid); await q.answer()
        await show_main_menu(update, context, 0)

    elif data.startswith("vis_specific_select:"):
        target_id = int(data.split(":")[1])
        st = get_user_state(uid)
        if not st: return
        sdata = st['data']
        try:
            ins = {"name": sdata['name'], "visibility": "specific_uadmin", "btn_type": "callback", "color": "", "emoji": "", "created_by": int(uid), "visible_to_user_id": target_id}
            supabase.table("buttons").insert(ins).execute()
            await q.edit_message_text(f"✅ Button '{sdata['name']}' created for UAdmin {target_id}")
        except Exception as e:
            await q.edit_message_text(f"Error: {e}")
        clear_user_state(uid); await q.answer()
        await show_main_menu(update, context, 0)

    # ---- ADMIN PANEL ----
    elif data.startswith("admin_"):
        if data=="admin_gen_key":
            if not is_owner(uid): await q.answer("Owner only"); return
            new_key = generate_key()
            try: supabase.table("access_keys").insert({"key": new_key, "is_used": False, "key_type": "normal"}).execute()
            except: supabase.table("access_keys").insert({"key": new_key, "is_used": False}).execute()
            await q.edit_message_text(f"✅ Normal Key:\n`{new_key}`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_gen_uadmin_key":
            if not is_owner(uid): await q.answer("Owner only"); return
            new_key = generate_uadmin_key()
            set_user_state(uid, "awaiting_uadmin_nickname", {"key": new_key})
            await q.edit_message_text(f"Generated UAdmin Key:\n`{new_key}`\n\nAb iske liye Nickname bhejo jaise `Rohit Bhai`", parse_mode="Markdown")
        elif data=="admin_list_keys":
            if not is_owner(uid): return
            res = supabase.table("access_keys").select("*").order("created_at", desc=True).limit(20).execute()
            txt="🔑 Keys (20):\n\n"
            for k in res.data:
                status="Used" if k['is_used'] else "Unused"
                txt+=f"{k['key']} - {status} by {k.get('used_by','-')} Nick:{k.get('nickname','-')}\n"
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_add_button":
            set_user_state(uid, "awaiting_new_button_name", {})
            await q.edit_message_text("📝 Send new button NAME:")
        elif data=="admin_manage_list":
            btns = get_manage_buttons_for_user(uid)
            if not btns:
                await q.edit_message_text("No buttons"); return
            rows=[]
            for b in btns[:30]:
                rows.append([InlineKeyboardButton(b['name'], callback_data=f"manage_btn:{b['id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("🗂 Select button:", reply_markup=InlineKeyboardMarkup(rows))
        elif data=="admin_add_coadmin":
            if not is_owner(uid): return
            set_user_state(uid, "awaiting_coadmin_id", {})
            await q.edit_message_text("👥 Send Co-Admin User ID:")
        elif data=="admin_list_coadmin":
            if not is_owner(uid): return
            res = supabase.table("co_admins").select("*").execute()
            txt="👥 Co-Admins:\n"
            for c in res.data: txt+=f"ID: {c['user_id']}\n"
            await q.edit_message_text(txt or "None", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_list_uadmins":
            if role not in ("owner","co_admin"): await q.answer("Owner/Co-Owner only"); return
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("No User Admins", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
                return
            rows=[]
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"uadmin_view:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("👥 User Admins (Nickname):", reply_markup=InlineKeyboardMarkup(rows))
        elif data=="admin_panel":
            await show_admin_panel(update, context)
        await q.answer()

    elif data.startswith("uadmin_view:"):
        target_id = int(data.split(":")[1])
        ua_res = supabase.table("user_admins").select("*").eq("user_id", target_id).execute()
        if not ua_res.data: await q.answer("Not found"); return
        ua = ua_res.data[0]
        nick = ua.get('nickname') or "No Nickname"
        kb = [
            [InlineKeyboardButton("📂 View Buttons", callback_data=f"uadmin_view_buttons:{target_id}")],
            [InlineKeyboardButton("✏️ Set Nickname", callback_data=f"uadmin_set_nick:{target_id}")],
            [InlineKeyboardButton("❌ Delete UAdmin", callback_data=f"uadmin_del:{target_id}")],
            [InlineKeyboardButton("Back", callback_data="admin_list_uadmins")],
        ]
        await q.edit_message_text(f"👤 UAdmin: {nick}\nID: {target_id}\nCreated by: {ua.get('created_by')}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("uadmin_view_buttons:"):
        target_id = int(data.split(":")[1])
        btns = supabase.table("buttons").select("*").eq("created_by", target_id).execute().data
        if not btns:
            await q.edit_message_text(f"No buttons by UAdmin {target_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"uadmin_view:{target_id}")]]))
            return
        rows=[]
        for b in btns[:30]:
            rows.append([InlineKeyboardButton(b['name'], callback_data=f"manage_btn:{b['id']}")])
        rows.append([InlineKeyboardButton("Back", callback_data=f"uadmin_view:{target_id}")])
        await q.edit_message_text(f"Buttons by UAdmin {target_id}:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("uadmin_set_nick:"):
        target_id = int(data.split(":")[1])
        set_user_state(uid, "awaiting_set_nickname", {"target_id": target_id})
        await q.edit_message_text(f"✏️ Send new nickname for UAdmin ID {target_id}:")

    elif data.startswith("uadmin_del:"):
        target_id = int(data.split(":")[1])
        if not is_owner(uid): await q.answer("Owner only"); return
        supabase.table("user_admins").delete().eq("user_id", target_id).execute()
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

    elif data=="m_done_upload":
        st = get_user_state(uid)
        upload_ids = st['data'].get('upload_msg_ids', []) if st and st['data'] else []
        chat_id = q.message.chat_id
        for mid in upload_ids: schedule_delete_30(context.bot, chat_id, mid)
        schedule_delete_30(context.bot, chat_id, q.message.message_id)
        clear_user_state(uid)
        m = await q.edit_message_text("✅ Upload finished. All upload messages will disappear in 30 seconds...")
        schedule_delete_30(context.bot, chat_id, m.message_id)
        await q.answer()

    elif data.startswith("m_listfiles:"):
        bid = int(data.split(":")[1])
        res = supabase.table("button_files").select("*").eq("button_id", bid).execute()
        if not res.data:
            await q.edit_message_text("No files", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
            return
        rows=[]
        for f in res.data[:20]:
            rows.append([InlineKeyboardButton(f"🗑 {f['file_type']} {f['id']}", callback_data=f"m_delfile:{f['id']}:{bid}")])
        rows.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
        await q.edit_message_text(f"Files for {bid}:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("m_delfile:"):
        _, fid, bid = data.split(":")
        supabase.table("button_files").delete().eq("id", int(fid)).execute()
        await q.answer("Deleted")
        await q.edit_message_text("Deleted", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

    elif data.startswith("m_delbtn:"):
        bid = int(data.split(":")[1])
        # owner, co-admin, ya button ka creator hi delete kar sakta hai
        btn_res = supabase.table("buttons").select("created_by").eq("id", bid).execute()
        if btn_res.data:
            c_by = btn_res.data[0].get('created_by')
            if role == "user_admin" and c_by and int(c_by)!= int(uid):
                await q.answer("❌ Sirf apna button delete kar sakte ho"); return
        supabase.table("buttons").delete().eq("id", bid).execute()
        await q.edit_message_text("✅ Button Deleted"); await q.answer()

    elif data.startswith("m_vis:"):
        bid = int(data.split(":")[1])
        rows=[]
        for name, val in VIS_OPTIONS:
            rows.append([InlineKeyboardButton(name, callback_data=f"m_vis_set:{bid}:{val}")])
        rows.append([InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")])
        await q.edit_message_text(f"👁 Choose new visibility for {bid}:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("m_vis_set:"):
        parts = data.split(":")
        bid = int(parts[1]); vis = parts[2]
        if vis == "specific_uadmin":
            uadmins = get_all_user_admins()
            if not uadmins:
                await q.edit_message_text("No UAdmins"); return
            rows=[]
            for ua in uadmins:
                nick = ua.get('nickname') or f"UAdmin {ua['user_id']}"
                rows.append([InlineKeyboardButton(f"{nick} (ID:{ua['user_id']})", callback_data=f"m_vis_specific:{bid}:{ua['user_id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data=f"m_vis:{bid}")])
            await q.edit_message_text("👤 Select UAdmin:", reply_markup=InlineKeyboardMarkup(rows))
            await q.answer(); return
        supabase.table("buttons").update({"visibility": vis, "visible_to_user_id": None}).eq("id", bid).execute()
        await q.answer(f"Vis -> {vis}")
        await q.edit_message_text(f"Visibility changed to {vis}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

    elif data.startswith("m_vis_specific:"):
        _, bid, target_id = data.split(":")
        bid=int(bid); target_id=int(target_id)
        supabase.table("buttons").update({"visibility": "specific_uadmin", "visible_to_user_id": target_id}).eq("id", bid).execute()
        await q.edit_message_text(f"Visibility -> Specific UAdmin {target_id}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    state_obj = get_user_state(uid)
    state = state_obj['state'] if state_obj else None
    sdata = state_obj['data'] if state_obj and state_obj.get('data') else {}

    if state == "awaiting_access_key":
        key_input = text.upper().strip()
        res = supabase.table("access_keys").select("*").eq("key", key_input).eq("is_used", False).execute()
        if res.data:
            row = res.data[0]
            is_uadmin_key = key_input.startswith("UADMIN-") or row.get('key_type') == 'uadmin'
            supabase.table("access_keys").update({"is_used": True, "used_by": int(uid)}).eq("key", key_input).execute()
            supabase.table("authorized_users").upsert({"user_id": int(uid)}, on_conflict="user_id").execute()
            if is_uadmin_key:
                nick = row.get('nickname') or f"UAdmin-{uid}"
                try:
                    supabase.table("user_admins").upsert({"user_id": int(uid), "nickname": nick, "created_by": int(OWNER_ID)}, on_conflict="user_id").execute()
                except Exception as e:
                    print(e)
                clear_user_state(uid)
                await update.effective_message.reply_text(f"✅ User Admin Access Granted!\nNickname: {nick}\nAb tum apna alag partition use karoge.")
            else:
                clear_user_state(uid)
                await update.effective_message.reply_text("✅ Access granted!")
            await show_main_menu(update, context, 0)
        else:
            await update.effective_message.reply_text("❌ Invalid or used key.")
        return

    if not is_authorized(uid):
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Send Access Key first.")
        return

    if state == "awaiting_uadmin_nickname":
        nick = text
        key = sdata.get('key')
        try:
            supabase.table("access_keys").insert({"key": key, "is_used": False, "nickname": nick, "key_type": "uadmin"}).execute()
        except:
            supabase.table("access_keys").insert({"key": key, "is_used": False}).execute()
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ UAdmin Key Created:\n`{key}`\nNickname: {nick}\nIsko bhejo, user auto UAdmin ban jayega.", parse_mode="Markdown")
        return

    if state == "awaiting_set_nickname":
        target_id = sdata.get('target_id')
        new_nick = text
        supabase.table("user_admins").update({"nickname": new_nick}).eq("user_id", target_id).execute()
        clear_user_state(uid)
        await update.effective_message.reply_text(f"✅ Nickname updated for ID {target_id} -> {new_nick}")
        return

    if state == "awaiting_new_button_name":
        if not text: await update.effective_message.reply_text("Send valid name"); return
        set_user_state(uid, "awaiting_new_button_vis", {"name": text})
        rows=[]
        for name, val in VIS_OPTIONS:
            rows.append([InlineKeyboardButton(name, callback_data=f"vis_{val}")])
        await update.effective_message.reply_text("👁 Choose visibility:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if state == "awaiting_coadmin_id":
        try:
            new_id = int(re.search(r'\d+', text).group())
            supabase.table("co_admins").upsert({"user_id": new_id, "added_by": int(uid)}, on_conflict="user_id").execute()
            supabase.table("authorized_users").upsert({"user_id": new_id}, on_conflict="user_id").execute()
            await update.effective_message.reply_text(f"✅ Co-Admin added: {new_id}")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid); return

    if state == "awaiting_file_upload":
        bid = sdata.get('button_id')
        upload_ids = sdata.get('upload_msg_ids', [])
        if text == "✅ Done":
            chat_id = update.effective_chat.id
            for mid in upload_ids: schedule_delete_30(context.bot, chat_id, mid)
            clear_user_state(uid)
            m = await update.effective_message.reply_text("✅ Upload finished. 30 sec me delete...")
            schedule_delete_30(context.bot, chat_id, m.message_id)
            schedule_delete_30(context.bot, chat_id, update.effective_message.message_id)
            return
        msg = update.effective_message
        upload_ids.append(msg.message_id)
        file_info=None
        if msg.photo:
            p=msg.photo[-1]
            file_info={"file_id": p.file_id, "file_unique_id": p.file_unique_id, "file_type": "photo", "caption": msg.caption or ""}
        elif msg.document:
            file_info={"file_id": msg.document.file_id, "file_unique_id": msg.document.file_unique_id, "file_type": "document", "caption": msg.caption or msg.document.file_name or ""}
        elif msg.video:
            file_info={"file_id": msg.video.file_id, "file_unique_id": msg.video.file_unique_id, "file_type": "video", "caption": msg.caption or ""}
        elif msg.audio:
            file_info={"file_id": msg.audio.file_id, "file_unique_id": msg.audio.file_unique_id, "file_type": "audio", "caption": msg.caption or ""}
        elif msg.voice:
            file_info={"file_id": msg.voice.file_id, "file_unique_id": msg.voice.file_unique_id, "file_type": "voice", "caption": msg.caption or ""}
        elif msg.video_note:
            file_info={"file_id": msg.video_note.file_id, "file_unique_id": msg.video_note.file_unique_id, "file_type": "video_note", "caption": ""}
        elif msg.sticker:
            file_info={"file_id": msg.sticker.file_id, "file_unique_id": msg.sticker.file_unique_id, "file_type": "sticker", "caption": ""}
        elif text and text!="✅ Done":
            file_info={"file_id": f"text_{uuid.uuid4()}", "file_unique_id": f"textu_{uuid.uuid4()}", "file_type": "text", "caption": text}
        if file_info:
            try:
                supabase.table("button_files").insert({
                    "button_id": bid, "file_id": file_info['file_id'],
                    "file_unique_id": file_info['file_unique_id'],
                    "file_type": file_info['file_type'], "caption": file_info['caption']
                }).execute()
                kb_done = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]])
                confirm = await update.effective_message.reply_text(f"✅ Added {file_info['file_type']}. Niche Done dabao ya aur bhejo.", reply_markup=kb_done)
                upload_ids.append(confirm.message_id)
                sdata['upload_msg_ids']=upload_ids
                set_user_state(uid, "awaiting_file_upload", sdata)
            except Exception as e:
                await update.effective_message.reply_text(f"Error saving: {e}")
        return

# ---------- Setup ----------
tg_app = Application.builder().token(BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start_handler))
tg_app.add_handler(CallbackQueryHandler(callback_handler))
tg_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(tg_app.initialize())
loop.run_until_complete(tg_app.start())

@app.route("/")
def home(): return "Bot Running - UADMIN + Nickname + Visibility OK"

@app.route("/keep-alive")
def keep_alive():
    try:
        supabase.table("buttons").select("id").limit(1).execute()
        return jsonify({"status":"ok"}),200
    except Exception as e:
        return jsonify({"error":str(e)}),500

if __name__ == "__main__":
    def run_flask():
        port=int(os.getenv("PORT",5000))
        app.run(host="0.0.0.0", port=port, use_reloader=False)
    threading.Thread(target=run_flask, daemon=True).start()
    async def start_polling():
        try: await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except: pass
        await tg_app.updater.start_polling(drop_pending_updates=True)
        print(f"✅ Polling started Owner {OWNER_ID}")
    loop.run_until_complete(start_polling())
    loop.run_forever()
