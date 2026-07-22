# app.py - SINGLE FILE MERGED - POLLING 24/7 UPTIMEROBOT (NO GUNICORN)
# Requirements: pip install Flask python-telegram-bot>=21.4 supabase python-dotenv
# ENV: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY (service_role), OWNER_ID
# Start: python app.py

import os, re, random, string, asyncio, json, uuid, threading
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
OWNER_ID = str(os.getenv("OWNER_ID", "")).strip()

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not OWNER_ID:
    print("WARNING: ENV missing!")

# Supabase Client (merged)
from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Telegram Imports
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, CopyTextButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

app = Flask(__name__)

# ---------- Helpers ----------
def clean_button_text(text: str) -> str:
    if not text: return ""
    t = text.strip()
    # Remove emoji/lock if present for matching
    t = re.sub(r'[^\w\s\-\_\.\(\)\[\]\{\}]+', '', t, flags=re.UNICODE).strip()
    return t

def is_owner(uid): return str(uid) == OWNER_ID

def is_co_admin(uid):
    try:
        r = supabase.table("co_admins").select("user_id").eq("user_id", int(uid)).execute()
        return len(r.data) > 0
    except: return False

def is_authorized(uid):
    if is_owner(uid): return True
    if is_co_admin(uid): return True
    try:
        r = supabase.table("authorized_users").select("user_id").eq("user_id", int(uid)).execute()
        return len(r.data) > 0
    except: return False

def get_user_state(uid):
    try:
        r = supabase.table("user_states").select("*").eq("user_id", int(uid)).execute()
        return r.data[0] if r.data else None
    except: return None

def set_user_state(uid, state, data=None):
    if data is None: data = {}
    supabase.table("user_states").upsert({
        "user_id": int(uid),
        "state": state,
        "data": data,
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="user_id").execute()

def clear_user_state(uid):
    try: supabase.table("user_states").delete().eq("user_id", int(uid)).execute()
    except: pass

def generate_key():
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"KEY-{rand}"

# ---------- Auto Delete ----------
async def auto_delete_message(bot, chat_id, message_id, delay=15):
    await asyncio.sleep(delay)
    try: await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

def schedule_delete(bot, chat_id, message_id):
    asyncio.create_task(auto_delete_message(bot, chat_id, message_id, 15))

# ---------- Button Rendering Logic (COLOURFUL) ----------
def build_inline_button(btn):
    name = btn['name'] # ONLY admin name, no emoji/number
    btype = (btn.get('btn_type') or 'callback').lower()
    try:
        if btype == 'copy' and btn.get('copy_value'):
            return InlineKeyboardButton(text=name, copy_text=CopyTextButton(text=btn['copy_value']))
        elif btype == 'url' and btn.get('url'):
            return InlineKeyboardButton(text=name, url=btn['url'])
        else:
            return InlineKeyboardButton(text=name, callback_data=f"view_btn:{btn['id']}:0")
    except Exception as e:
        print(f"CopyTextButton fallback: {e}")
        if btype == 'url' and btn.get('url'):
            return InlineKeyboardButton(text=name, url=btn['url'])
        return InlineKeyboardButton(text=name, callback_data=f"view_btn:{btn['id']}:0")

PER_PAGE = 15

def get_buttons_paginated(page, show_owner_only=False):
    try:
        offset = page * PER_PAGE
        q = supabase.table("buttons").select("*").order("name", desc=False).range(offset, offset+PER_PAGE-1)
        if not show_owner_only:
            q = q.neq("visibility", "owner_only")
        res = q.execute()
        count_q = supabase.table("buttons").select("id", count="exact")
        if not show_owner_only:
            count_q = count_q.neq("visibility", "owner_only")
        total = count_q.execute().count or 0
        return res.data, total
    except Exception as e:
        print("get_buttons error", e)
        return [], 0

def get_all_buttons_for_manage(show_all=False):
    try:
        q = supabase.table("buttons").select("*").order("name")
        if not show_all:
            q = q.neq("visibility", "owner_only")
        return q.execute().data
    except: return []

# ---------- Core Send Functions ----------
async def send_button_files(update, context, button):
    chat_id = update.effective_chat.id
    try:
        files_res = supabase.table("button_files").select("*").eq("button_id", button['id']).execute()
        files = files_res.data
        if not files:
            msg = await context.bot.send_message(chat_id, f"📭 '{button['name']}' is empty.")
            schedule_delete(context.bot, chat_id, msg.message_id)
            return
        for f in files:
            try:
                caption = (f.get('caption') or "") + "\n\n⏳ This message will auto-delete in 15 seconds... Click again to view."
                ftype = f.get('file_type','document')
                fid = f.get('file_id')
                if ftype == 'text' or not fid or fid.startswith('text_'):
                    m = await context.bot.send_message(chat_id, text=f.get('caption') or "No content" + "\n\n⏳ Auto-delete in 15 sec...")
                elif ftype == 'photo':
                    m = await context.bot.send_photo(chat_id, photo=fid, caption=caption)
                elif ftype == 'video':
                    m = await context.bot.send_video(chat_id, video=fid, caption=caption)
                elif ftype == 'audio':
                    m = await context.bot.send_audio(chat_id, audio=fid, caption=caption)
                elif ftype == 'voice':
                    m = await context.bot.send_voice(chat_id, voice=fid, caption=caption)
                elif ftype == 'video_note':
                    m = await context.bot.send_video_note(chat_id, video_note=fid)
                elif ftype == 'sticker':
                    m = await context.bot.send_sticker(chat_id, sticker=fid)
                else:
                    m = await context.bot.send_document(chat_id, document=fid, caption=caption)
                schedule_delete(context.bot, chat_id, m.message_id)
            except Exception as e:
                print(f"send file error {e}")
                await context.bot.send_message(chat_id, f"⚠ Error sending file: {e}")
    except Exception as e:
        await context.bot.send_message(chat_id, f"Error: {e}")

async def show_main_menu(update, context, page=0):
    uid = update.effective_user.id
    show_owner = is_owner(uid)
    buttons, total = get_buttons_paginated(page, show_owner_only=show_owner)
    total_pages = max(1, (total + PER_PAGE - 1)//PER_PAGE)
    reply_rows = []
    row = []
    for b in buttons:
        row.append(b['name'])
        if len(row)==2:
            reply_rows.append(row); row=[]
    if row: reply_rows.append(row)
    nav = []
    if page>0: nav.append("⬅ Prev Page")
    if page < total_pages-1: nav.append("Next Page ➡")
    if nav: reply_rows.append(nav)
    if is_owner(uid) or is_co_admin(uid):
        reply_rows.append(["🛠 Admin Panel"])
    else:
        reply_rows.append(["🏠 Main Menu"])
    reply_kb = ReplyKeyboardMarkup(reply_rows, resize_keyboard=True)
    inline_rows = []
    r = []
    for b in buttons:
        r.append(build_inline_button(b))
        if len(r)==2:
            inline_rows.append(r); r=[]
    if r: inline_rows.append(r)
    pag_row = []
    if page>0: pag_row.append(InlineKeyboardButton("⬅ Prev", callback_data=f"main_page:{page-1}"))
    if page < total_pages-1: pag_row.append(InlineKeyboardButton("Next ➡", callback_data=f"main_page:{page+1}"))
    if pag_row: inline_rows.append(pag_row)
    context.user_data['main_page'] = page
    text = f"📂 Main Menu (Page {page+1}/{total_pages}) - {total} buttons\nSelect any button:"
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
            await update.callback_query.answer()
            await context.bot.send_message(update.effective_chat.id, "⌨ Keyboard updated", reply_markup=reply_kb)
        else:
            await context.bot.send_message(update.effective_chat.id, "⌨ Menu", reply_markup=reply_kb)
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(inline_rows))
    except Exception as e:
        print(e)
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(inline_rows))

async def show_admin_panel(update, context):
    uid = update.effective_user.id
    if not (is_owner(uid) or is_co_admin(uid)):
        await update.effective_message.reply_text("❌ Admin only")
        return
    if is_owner(uid):
        kb = [
            [InlineKeyboardButton("🔑 Generate New Key", callback_data="admin_gen_key")],
            [InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys")],
            [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("👥 Add Co-Admin", callback_data="admin_add_coadmin")],
            [InlineKeyboardButton("📜 List Co-Admins", callback_data="admin_list_coadmin")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
        ]
    await update.effective_message.reply_text("🛠 Admin Panel", reply_markup=InlineKeyboardMarkup(kb))

# ---------- Handlers ----------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_authorized(uid):
        clear_user_state(uid)
        await show_main_menu(update, context, 0)
    else:
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text(
            "🔐 Welcome! Please send your Access Key to continue.\nFormat: KEY-XXXXXXXX\nContact owner for key."
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = update.effective_user.id
    if data.startswith("view_btn:"):
        _, bid, page = data.split(":")
        try:
            res = supabase.table("buttons").select("*").eq("id", int(bid)).execute()
            if not res.data:
                await q.answer("Button not found"); return
            btn = res.data[0]
            if btn['visibility']=='owner_only' and not is_owner(uid):
                await q.answer("❌ Owner only"); return
            await q.answer()
            await send_button_files(update, context, btn)
        except Exception as e:
            await q.answer(f"Error {e}")
    elif data.startswith("main_page:"):
        page = int(data.split(":")[1])
        await show_main_menu(update, context, page)
    elif data.startswith("admin_"):
        if not (is_owner(uid) or is_co_admin(uid)):
            await q.answer("Admin only"); return
        if data=="admin_gen_key":
            if not is_owner(uid): await q.answer("Owner only"); return
            new_key = generate_key()
            supabase.table("access_keys").insert({"key": new_key, "is_used": False}).execute()
            await q.edit_message_text(f"✅ New Key Generated:\n`{new_key}`\nOne-time use only.", parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_list_keys":
            if not is_owner(uid): return
            res = supabase.table("access_keys").select("*").order("created_at", desc=True).limit(20).execute()
            txt = "🔑 Keys (last 20):\n\n"
            for k in res.data:
                status = "✅ Used" if k['is_used'] else "🟢 Unused"
                txt += f"{k['key']} - {status} by {k.get('used_by','-')}\n"
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_add_button":
            set_user_state(uid, "awaiting_new_button_name", {})
            await q.edit_message_text("📝 Send new button NAME (exact name that will show, no emoji auto):")
        elif data=="admin_manage_list":
            show_all = is_owner(uid)
            btns = get_all_buttons_for_manage(show_all)
            if not btns:
                await q.edit_message_text("No buttons")
                return
            rows = []
            for b in btns[:30]:
                rows.append([InlineKeyboardButton(b['name'], callback_data=f"manage_btn:{b['id']}")])
            rows.append([InlineKeyboardButton("Back", callback_data="admin_panel")])
            await q.edit_message_text("🗂 Select button to manage:", reply_markup=InlineKeyboardMarkup(rows))
        elif data=="admin_add_coadmin":
            if not is_owner(uid): return
            set_user_state(uid, "awaiting_coadmin_id", {})
            await q.edit_message_text("👥 Send Co-Admin User ID (numeric):")
        elif data=="admin_list_coadmin":
            if not is_owner(uid): return
            res = supabase.table("co_admins").select("*").execute()
            txt = "👥 Co-Admins:\n"
            for c in res.data: txt += f"ID: {c['user_id']} added by {c['added_by']}\n"
            await q.edit_message_text(txt or "None", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin_panel")]]))
        elif data=="admin_panel":
            await q.edit_message_text("🛠 Admin Panel", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Generate New Key", callback_data="admin_gen_key")],
                [InlineKeyboardButton("📋 List Keys", callback_data="admin_list_keys")],
                [InlineKeyboardButton("➕ Add New Button", callback_data="admin_add_button")],
                [InlineKeyboardButton("🗂 Manage Buttons", callback_data="admin_manage_list")],
                [InlineKeyboardButton("👥 Add Co-Admin", callback_data="admin_add_coadmin")],
                [InlineKeyboardButton("📜 List Co-Admins", callback_data="admin_list_coadmin")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_page:0")],
            ]))
        await q.answer()
    elif data.startswith("manage_btn:"):
        bid = int(data.split(":")[1])
        rows = [
            [InlineKeyboardButton("📤 Add Files", callback_data=f"m_addfile:{bid}")],
            [InlineKeyboardButton("📄 List/Delete Files", callback_data=f"m_listfiles:{bid}")],
            [InlineKeyboardButton("🎨 Change Colour/Type", callback_data=f"m_editcolor:{bid}")],
            [InlineKeyboardButton("👁 Visibility", callback_data=f"m_vis:{bid}")],
            [InlineKeyboardButton("❌ Delete Button", callback_data=f"m_delbtn:{bid}")],
            [InlineKeyboardButton("Back", callback_data="admin_manage_list")],
        ]
        await q.edit_message_text(f"Manage Button ID {bid}", reply_markup=InlineKeyboardMarkup(rows))
        await q.answer()
    elif data.startswith("m_addfile:"):
        bid = int(data.split(":")[1])
        set_user_state(uid, "awaiting_file_upload", {"button_id": bid})
        await q.edit_message_text(f"📤 Send ANY files (photo, video, doc, text) for button {bid}.\nSend multiple, then type ✅ Done",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="m_done_upload")]]))
        await q.answer()
    elif data=="m_done_upload":
        clear_user_state(uid)
        await q.edit_message_text("✅ Upload finished.")
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
        if is_co_admin(uid) and not is_owner(uid):
            await q.answer("❌ Co-Admin cannot delete")
            return
        supabase.table("button_files").delete().eq("id", int(fid)).execute()
        await q.answer("Deleted")
        await q.edit_message_text("Deleted", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))
    elif data.startswith("m_delbtn:"):
        bid = int(data.split(":")[1])
        if is_co_admin(uid) and not is_owner(uid):
            await q.answer("❌ Co-Admin cannot delete button")
            return
        supabase.table("buttons").delete().eq("id", bid).execute()
        await q.edit_message_text("✅ Button Deleted")
        await q.answer()
    elif data.startswith("m_editcolor:"):
        bid = int(data.split(":")[1])
        set_user_state(uid, "awaiting_new_button_color", {"button_id": bid, "edit": True})
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Red", callback_data="color_Red"), InlineKeyboardButton("Green", callback_data="color_Green")],
            [InlineKeyboardButton("Blue", callback_data="color_Blue"), InlineKeyboardButton("Purple", callback_data="color_Purple")],
            [InlineKeyboardButton("Orange", callback_data="color_Orange"), InlineKeyboardButton("Black", callback_data="color_Black")],
        ])
        await q.edit_message_text("Choose new color (saved in DB, button text stays same):", reply_markup=kb)
    elif data.startswith("color_"):
        color = data.split("_")[1]
        st = get_user_state(uid)
        if st and st['state']=="awaiting_new_button_color" and st['data'].get('edit'):
            bid = st['data']['button_id']
            supabase.table("buttons").update({"color": color}).eq("id", bid).execute()
            clear_user_state(uid)
            await q.edit_message_text(f"✅ Color changed to {color}")
        await q.answer()
    elif data.startswith("m_vis:"):
        bid = int(data.split(":")[1])
        res = supabase.table("buttons").select("visibility").eq("id", bid).execute()
        if res.data:
            new_vis = "owner_only" if res.data[0]['visibility']=="all" else "all"
            supabase.table("buttons").update({"visibility": new_vis}).eq("id", bid).execute()
            await q.answer(f"Visibility -> {new_vis}")
            await q.edit_message_text(f"Visibility changed to {new_vis}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=f"manage_btn:{bid}")]]))

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
            supabase.table("access_keys").update({"is_used": True, "used_by": int(uid)}).eq("key", key_input).execute()
            supabase.table("authorized_users").upsert({"user_id": int(uid)}, on_conflict="user_id").execute()
            clear_user_state(uid)
            await update.effective_message.reply_text("✅ Access granted!")
            await show_main_menu(update, context, 0)
        else:
            await update.effective_message.reply_text("❌ Invalid or used key. Try again or contact owner.")
        return
    if not is_authorized(uid):
        set_user_state(uid, "awaiting_access_key", {})
        await update.effective_message.reply_text("🔐 Send Access Key first. Format KEY-XXXXXXXX")
        return
    if state == "awaiting_new_button_name":
        if not text:
            await update.effective_message.reply_text("Send valid name")
            return
        set_user_state(uid, "awaiting_new_button_vis", {"name": text})
        kb = ReplyKeyboardMarkup([["Public (All Users)", "Owner Only"]], resize_keyboard=True, one_time_keyboard=True)
        await update.effective_message.reply_text("👁 Choose visibility:", reply_markup=kb)
        return
    if state == "awaiting_new_button_vis":
        vis = "all" if "Public" in text else "owner_only"
        sdata['visibility'] = vis
        set_user_state(uid, "awaiting_new_button_type", sdata)
        kb = ReplyKeyboardMarkup([["Copy = Green", "URL = Purple/Red", "Callback = Normal"]], resize_keyboard=True, one_time_keyboard=True)
        await update.effective_message.reply_text("🎨 Choose button type (for colour):\nCopy = GREEN\nURL = PURPLE/RED", reply_markup=kb)
        return
    if state == "awaiting_new_button_type":
        if "Copy" in text: btype="copy"
        elif "URL" in text: btype="url"
        else: btype="callback"
        sdata['btn_type']=btype
        if btype=="copy":
            set_user_state(uid, "awaiting_new_button_copy", sdata)
            await update.effective_message.reply_text("📋 Send COPY text (jo copy hoga):")
        elif btype=="url":
            set_user_state(uid, "awaiting_new_button_url", sdata)
            await update.effective_message.reply_text("🔗 Send URL (https://...):")
        else:
            set_user_state(uid, "awaiting_new_button_color", sdata)
            kb = ReplyKeyboardMarkup([["Red","Green","Blue","Purple","Orange","Black"]], resize_keyboard=True)
            await update.effective_message.reply_text("🎨 Choose color for DB (button text will stay exact name):", reply_markup=kb)
        return
    if state == "awaiting_new_button_copy":
        sdata['copy_value']=text
        set_user_state(uid, "awaiting_new_button_color", sdata)
        kb = ReplyKeyboardMarkup([["Red","Green","Blue","Purple","Orange","Black"]], resize_keyboard=True)
        await update.effective_message.reply_text("🎨 Choose color:", reply_markup=kb)
        return
    if state == "awaiting_new_button_url":
        sdata['url']=text
        set_user_state(uid, "awaiting_new_button_color", sdata)
        kb = ReplyKeyboardMarkup([["Red","Green","Blue","Purple","Orange","Black"]], resize_keyboard=True)
        await update.effective_message.reply_text("🎨 Choose color:", reply_markup=kb)
        return
    if state == "awaiting_new_button_color":
        color = text if text else "Green"
        if sdata.get('edit'):
            pass
        else:
            try:
                ins = {
                    "name": sdata['name'],
                    "visibility": sdata.get('visibility','all'),
                    "btn_type": sdata.get('btn_type','callback'),
                    "url": sdata.get('url'),
                    "copy_value": sdata.get('copy_value'),
                    "color": color,
                    "emoji": ""
                }
                supabase.table("buttons").insert(ins).execute()
                await update.effective_message.reply_text(f"✅ Button '{sdata['name']}' created! Colour: {color}, Type: {sdata.get('btn_type')}")
            except Exception as e:
                if "unique" in str(e).lower():
                    await update.effective_message.reply_text("❌ Button name already exists! Try different name.")
                else:
                    await update.effective_message.reply_text(f"Error: {e}")
        clear_user_state(uid)
        await show_main_menu(update, context, 0)
        return
    if state == "awaiting_coadmin_id":
        try:
            new_id = int(re.search(r'\d+', text).group())
            supabase.table("co_admins").upsert({"user_id": new_id, "added_by": int(uid)}, on_conflict="user_id").execute()
            supabase.table("authorized_users").upsert({"user_id": new_id}, on_conflict="user_id").execute()
            await update.effective_message.reply_text(f"✅ Co-Admin added: {new_id}")
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e} - send numeric ID")
        clear_user_state(uid)
        return
    if state == "awaiting_file_upload":
        bid = sdata.get('button_id')
        if text == "✅ Done":
            clear_user_state(uid)
            await update.effective_message.reply_text("✅ Upload finished")
            return
        msg = update.effective_message
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
        elif text and text!="✅ Done":
            file_info = {"file_id": f"text_{uuid.uuid4()}", "file_unique_id": f"textu_{uuid.uuid4()}", "file_type": "text", "caption": text}
        if file_info:
            try:
                supabase.table("button_files").insert({
                    "button_id": bid,
                    "file_id": file_info['file_id'],
                    "file_unique_id": file_info['file_unique_id'],
                    "file_type": file_info['file_type'],
                    "caption": file_info['caption']
                }).execute()
                await update.effective_message.reply_text(f"✅ Added {file_info['file_type']}. Send more or type ✅ Done")
            except Exception as e:
                await update.effective_message.reply_text(f"Error saving: {e}")
        return
    if text in ["⬅ Prev Page", "Next Page ➡"]:
        page = context.user_data.get('main_page',0)
        if "Prev" in text: page = max(0, page-1)
        else: page+=1
        await show_main_menu(update, context, page)
        return
    if text == "🛠 Admin Panel":
        await show_admin_panel(update, context)
        return
    if text == "🏠 Main Menu":
        await show_main_menu(update, context, 0)
        return
    if text:
        try:
            show_all = is_owner(uid)
            all_btns = get_all_buttons_for_manage(show_all)
            matched = None
            for b in all_btns:
                if b['name'].lower().strip() == text.lower().strip() or clean_button_text(b['name']).lower() == clean_button_text(text).lower():
                    matched = b
                    break
            if matched:
                if matched['visibility']=='owner_only' and not is_owner(uid):
                    await update.effective_message.reply_text("❌ Owner only button")
                    return
                await send_button_files(update, context, matched)
                return
        except Exception as e:
            print(e)
    if update.effective_message.photo or update.effective_message.document:
        await update.effective_message.reply_text("To add file, go to Admin Panel > Manage Buttons > Add Files")

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
    return "Bot Running - Polling 24/7 - UptimeRobot OK"

@app.route("/keep-alive")
def keep_alive():
    try:
        supabase.table("buttons").select("id").limit(1).execute()
        return jsonify({"status": "ok", "msg": "Supabase pinged"}), 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, tg_app.bot)
        loop.run_until_complete(tg_app.process_update(update))
    except Exception as e:
        print(f"Webhook error {e}")
    return "ok"

@app.route("/setwebhook")
def set_webhook():
    url = request.args.get("url")
    if not url:
        return "Provide?url=https://your-app.onrender.com", 400
    full = f"{url.rstrip('/')}/webhook/{BOT_TOKEN}"
    async def _set():
        await tg_app.bot.set_webhook(full)
    loop.run_until_complete(_set())
    return f"Webhook set to {full}"

# ---------- POLLING 24/7 RUNNER - NO GUNICORN ----------
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
        print(f"✅ Polling started! Owner {OWNER_ID} auto access, no key needed. UptimeRobot 24/7 live")

    loop.run_until_complete(start_polling())
    loop.run_forever()

# ---------- SUPABASE SQL ----------
"""
create table access_keys (key text primary key, is_used bool default false, used_by bigint, created_at timestamp default now());
create table authorized_users (user_id bigint primary key);
create table co_admins (user_id bigint primary key, added_by bigint, added_at timestamp default now());
create table buttons (
 id bigserial primary key,
 name text unique not null,
 visibility text default 'all' check (visibility in ('all','owner_only')),
 color text,
 emoji text default '',
 btn_type text default 'callback' check (btn_type in ('copy','url','callback')),
 url text,
 copy_value text,
 created_at timestamp default now()
);
create table button_files (
 id bigserial primary key,
 button_id bigint references buttons(id) on delete cascade,
 file_id text not null,
 file_unique_id text not null,
 file_type text,
 caption text,
 price text,
 country text,
 custom_data jsonb,
 created_at timestamp default now()
);
create table user_states (
 user_id bigint primary key,
 state text,
 data jsonb,
 updated_at timestamp default now()
);
"""
