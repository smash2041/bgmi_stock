import asyncio
import html as html_lib
import os
import re
import shutil
import tempfile
import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

try:
    from telethon import TelegramClient, events
    from telethon.errors import FloodWaitError
    from telethon.sessions import StringSession
except Exception as e:
    TelegramClient = None
    events = None
    FloodWaitError = None
    StringSession = None


SCOPE_CO_OWNERS = "co_owners"
SCOPE_ALL_UADMINS = "all_uadmins"
SCOPE_SPECIFIC_UADMIN = "specific_uadmin"

STATE_PERM_COMMAND = "awaiting_dm_perm_command"
STATE_PERM_EDIT_COMMAND = "awaiting_dm_perm_edit_command"
STATE_NEW_BUTTON_NAME = "awaiting_dm_new_button_name"


async def init_db(db):
    await db.aexecute("""CREATE TABLE IF NOT EXISTS dark_mode_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS dark_mode_perms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        command TEXT NOT NULL,
        scope TEXT NOT NULL,
        target_user_id INTEGER,
        created_by INTEGER,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(command, scope, target_user_id)
    )""")
    await db.aexecute("""CREATE TABLE IF NOT EXISTS dark_mode_requests (
        request_id TEXT PRIMARY KEY,
        source_user_id INTEGER,
        source_chat_id INTEGER,
        source_message_id INTEGER,
        telethon_message_id INTEGER,
        botb_message_id INTEGER,
        command_text TEXT,
        status TEXT,
        preview_chat_id INTEGER,
        preview_message_id INTEGER,
        created_at TEXT,
        updated_at TEXT
    )""")


@dataclass
class DarkModeRequest:
    request_id: str
    user_id: int
    chat_id: int
    command_text: str
    source_message_id: Optional[int] = None
    telethon_message_id: Optional[int] = None
    botb_message_id: Optional[int] = None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)


@dataclass
class PreviewPayload:
    request_id: str
    user_id: int
    chat_id: int
    message_id: int
    file_info: Dict[str, Any]
    created_at: float = field(default_factory=time.time)


class DarkMode:
    def __init__(
        self,
        *,
        db,
        owner_id: str,
        backup_channel_id: str,
        get_user_role: Callable[[int], Awaitable[str]],
        is_authorized: Callable[[int], Awaitable[bool]],
        get_all_user_admins: Callable[[], Awaitable[list]],
        get_manage_buttons_for_user: Callable[[int], Awaitable[list]],
        can_add_files_to_button: Callable[[int, dict, str], Awaitable[bool]],
        get_button_by_id: Callable[[int], Awaitable[Optional[dict]]],
        get_all_buttons_cached: Callable[..., Awaitable[list]],
        set_user_state: Callable[[int, str, dict], Awaitable[None]],
        clear_user_state: Callable[[int], Awaitable[None]],
        invalidate_button_cache: Callable[[], None],
        backup_caption_with_button: Callable[[str, str], str],
        refresh_button_pdf_backup: Callable[[Any, int, Optional[dict]], Awaitable[None]],
        schedule_pdf_rebuild: Optional[Callable[[Any, int, Optional[dict]], None]],
        pdf_merge_types: set,
    ):
        self.db = db
        self.owner_id = str(owner_id)
        self.backup_channel_id = str(backup_channel_id or "").strip()
        self.get_user_role = get_user_role
        self.is_authorized = is_authorized
        self.get_all_user_admins = get_all_user_admins
        self.get_manage_buttons_for_user = get_manage_buttons_for_user
        self.can_add_files_to_button = can_add_files_to_button
        self.get_button_by_id = get_button_by_id
        self.get_all_buttons_cached = get_all_buttons_cached
        self.set_user_state = set_user_state
        self.clear_user_state = clear_user_state
        self.invalidate_button_cache = invalidate_button_cache
        self.backup_caption_with_button = backup_caption_with_button
        self.refresh_button_pdf_backup = refresh_button_pdf_backup
        self.schedule_pdf_rebuild = schedule_pdf_rebuild
        self.pdf_merge_types = pdf_merge_types

        self.bot = None
        self.client = None
        self.queue = asyncio.Queue()
        self.worker_task = None
        self.cleanup_task = None
        self.started = False

        # Command-permission cache: avoids a fresh DB round-trip on every single
        # command check while dark mode is ON. Invalidated on any perm add/edit/delete.
        self._perm_cache: Optional[list] = None
        self._perm_cache_ts: float = 0
        self._perm_cache_ttl: float = 60.0
        
        # In-Chat Temp Tracking Mapping
        self.typing_tasks: Dict[str, asyncio.Task] = {}
        self.temp_status_msgs: Dict[str, Any] = {} 

        self.requests: Dict[str, DarkModeRequest] = {}
        self.telethon_msg_to_request: Dict[int, str] = {}
        self.botb_msg_to_request: Dict[int, str] = {}
        self.previews: Dict[str, PreviewPayload] = {}
        self.final_tasks: Dict[str, asyncio.Task] = {}

        self.private_group_id = self._env_int("DARK_MODE_PRIVATE_GROUP_ID")
        self.bot_b_id = self._env_int("DARK_MODE_BOT_B_ID")
        self.queue_delay = float(os.getenv("DARK_MODE_QUEUE_DELAY", "0.9") or "0.9")
        self.final_settle_seconds = float(os.getenv("DARK_MODE_FINAL_SETTLE_SECONDS", "1.2") or "1.2")
        self.preview_seconds = int(os.getenv("DARK_MODE_PREVIEW_SECONDS", "30") or "30")
        raw_patterns = os.getenv("DARK_MODE_INTERIM_PATTERNS", "")
        patterns = raw_patterns.split(",") if raw_patterns else [
            "processing",
            "please wait",
            "wait",
            "loading",
            "fetching",
            "searching",
            "working",
            "hold on",
        ]
        self.interim_patterns = [p.strip().casefold() for p in patterns if p.strip()]

    def _env_int(self, key: str) -> Optional[int]:
        val = os.getenv(key, "").strip()
        if not val:
            return None
        try:
            return int(val)
        except Exception as e:
            return None

    def _is_owner(self, uid: int) -> bool:
        return str(uid) == self.owner_id

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalize_command(self, text: str) -> str:
        first = (text or "").strip().split(maxsplit=1)[0].strip()
        if not first.startswith("/"):
            first = "/" + first
        return first.casefold()

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(600)  # Check every 10 mins
                now = time.time()
                expired = []
                for req_id, req in list(self.requests.items()):
                    if now - req.created_at > 3600:  # Delete requests older than 1 hr
                        expired.append(req_id)
                for req_id in expired:
                    self._cleanup_request_tracking(req_id)
                if expired:
                    logging.info(f"Dark Mode Cleanup: Purged {len(expired)} stale memory leaks.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Dark mode cleanup loop error: {e}")

    async def start(self, bot=None):
        if bot is not None:
            self.bot = bot
        if self.started:
            return True
        if TelegramClient is None:
            logging.warning("Dark Mode disabled: telethon package is not installed.")
            return False

        api_id = self._env_int("TELETHON_API_ID")
        api_hash = os.getenv("TELETHON_API_HASH", "").strip()
        session_value = os.getenv("TELETHON_SESSION", "").strip()
        session_file = os.getenv("TELETHON_SESSION_FILE", "dark_mode_session").strip()
        if not api_id or not api_hash or not self.private_group_id or not self.bot_b_id:
            logging.warning("Dark Mode Telethon not started: TELETHON_API_ID/API_HASH, DARK_MODE_PRIVATE_GROUP_ID, DARK_MODE_BOT_B_ID required.")
            return False

        session_obj = session_file
        if session_value and StringSession is not None:
            session_obj = StringSession(session_value)
        else:
            session_path = session_file if session_file.endswith(".session") else f"{session_file}.session"
            if not os.path.exists(session_path):
                logging.warning("Dark Mode Telethon not started: TELETHON_SESSION string or existing TELETHON_SESSION_FILE is required.")
                return False

        try:
            self.client = TelegramClient(session_obj, api_id, api_hash)
            await self.client.start()
            self.client.add_event_handler(
                self._on_botb_new_message,
                events.NewMessage(chats=self.private_group_id, from_users=self.bot_b_id),
            )
            self.client.add_event_handler(
                self._on_botb_message_edited,
                events.MessageEdited(chats=self.private_group_id, from_users=self.bot_b_id),
            )
            self.worker_task = asyncio.create_task(self._queue_worker())
            self.cleanup_task = asyncio.create_task(self._cleanup_loop())
            self.started = True
            logging.info("Dark Mode Telethon started.")
            return True
        except Exception as e:
            logging.error(f"Dark Mode Telethon start failed: {e}")
            self.client = None
            self.started = False
            return False

    async def stop(self):
        if getattr(self, "worker_task", None):
            self.worker_task.cancel()
        if getattr(self, "cleanup_task", None):
            self.cleanup_task.cancel()
        if self.client:
            await self.client.disconnect()

    async def is_enabled(self) -> bool:
        cur = await self.db.aexecute("SELECT value FROM dark_mode_settings WHERE key = 'enabled'")
        row = cur.fetchone()
        return bool(row and str(row[0]) == "1")

    async def set_enabled(self, enabled: bool):
        await self.db.aexecute(
            "INSERT OR REPLACE INTO dark_mode_settings (key, value, updated_at) VALUES ('enabled', ?, ?)",
            ("1" if enabled else "0", self._now()),
        )

    async def owner_panel_label(self) -> str:
        return "Dark Mode: ON" if await self.is_enabled() else "Dark Mode: OFF"

    async def handle_callback(self, update, context) -> bool:
        q = update.callback_query
        data = q.data or ""
        uid = update.effective_user.id
        if not data.startswith("dm:"):
            return False

        if not self._is_owner(uid) and not data.startswith(("dm:add:", "dm:new:", "dm:pick:", "dm:save:")):
            await self._screen(q, "🔒 <b>Owner only.</b>")
            return True

        if data == "dm:panel":
            await self.clear_user_state(uid)
            await self._show_panel(q)
        elif data == "dm:toggle":
            enabled = not await self.is_enabled()
            await self.set_enabled(enabled)
            if enabled:
                started = await self.start(context.bot)
                if not started:
                    note = "Dark Mode ON ✅ — ⚠️ Lekin Telethon bridge start nahi hua (env config missing). Dark mode commands kaam nahi karenge jab tak env set na ho."
                    await self._show_panel(q, note=note)
                    return
            await self._show_panel(q, note=f"Dark Mode {'ON' if enabled else 'OFF'}")
        elif data == "dm:perms":
            await self.clear_user_state(uid)
            await self._show_perms(q)
        elif data == "dm:add_perm":
            await self.set_user_state(uid, STATE_PERM_COMMAND, {})
            await self._screen(q, "✍️ Send command name, example: <code>/get</code>")
        elif data.startswith("dm:perm_scope:"):
            scope = data.split(":", 2)[2]
            await self._save_perm_scope_from_state(q, uid, scope)
        elif data.startswith("dm:perm_uadmin:"):
            target_uid = int(data.split(":", 2)[2])
            await self._save_perm_from_state(q, uid, SCOPE_SPECIFIC_UADMIN, target_uid)
        elif data.startswith("dm:perm_view:"):
            await self._show_perm_detail(q, int(data.split(":", 2)[2]))
        elif data.startswith("dm:perm_delete:"):
            await self.db.aexecute("DELETE FROM dark_mode_perms WHERE id = ?", (int(data.split(":", 2)[2]),))
            self._invalidate_perm_cache()
            await self._show_perms(q, "Permission deleted.")
        elif data.startswith("dm:perm_edit_cmd:"):
            perm_id = int(data.split(":", 2)[2])
            await self.set_user_state(uid, STATE_PERM_EDIT_COMMAND, {"perm_id": perm_id})
            await self._screen(q, "✍️ Send new command, example: <code>/only</code>")
        elif data.startswith("dm:perm_edit_scope:"):
            await self._show_edit_scope(q, int(data.split(":", 2)[2]))
        elif data.startswith("dm:perm_scope_edit:"):
            _, _, perm_id, scope = data.split(":", 3)
            await self._update_perm_scope(q, int(perm_id), scope)
        elif data.startswith("dm:perm_uadmin_edit:"):
            _, _, perm_id, target_uid = data.split(":", 3)
            await self._update_perm(q, int(perm_id), None, SCOPE_SPECIFIC_UADMIN, int(target_uid))
        elif data.startswith("dm:add:"):
            await self._show_existing_buttons(q, update, context, data.split(":", 2)[2])
        elif data.startswith("dm:pick:"):
            _, _, request_id, bid = data.split(":", 3)
            await self._save_preview_to_button(q, context, request_id, int(bid))
        elif data.startswith("dm:new:"):
            request_id = data.split(":", 2)[2]
            if not await self._validate_preview_owner(q, request_id):
                return True
            await self.set_user_state(uid, STATE_NEW_BUTTON_NAME, {"request_id": request_id})
            await self._screen(q, "✍️ Send new folder name.")
        else:
            await self._screen(q, "❓ Unknown Dark Mode action.")
        return True

    async def handle_text_state(self, update, context, state: Optional[str], sdata: dict) -> bool:
        if not state:
            return False
        uid = update.effective_user.id
        text = (update.effective_message.text or "").strip()

        if state == STATE_PERM_COMMAND:
            if not self._is_owner(uid):
                await self.clear_user_state(uid)
                return True
            cmd = self._normalize_command(text)
            if not re.match(r"^/[a-zA-Z0-9_]{1,32}$", cmd):
                await update.effective_message.reply_text("Valid command bhejo, example: /get")
                return True
            await self.set_user_state(uid, STATE_PERM_COMMAND, {"command": cmd})
            await update.effective_message.reply_text(
                f"🎯 <b>{cmd}</b>\nKisko allow karna hai?",
                reply_markup=self._scope_markup("dm:perm_scope:"),
                parse_mode="HTML",
            )
            return True

        if state == STATE_PERM_EDIT_COMMAND:
            if not self._is_owner(uid):
                await self.clear_user_state(uid)
                return True
            cmd = self._normalize_command(text)
            if not re.match(r"^/[a-zA-Z0-9_]{1,32}$", cmd):
                await update.effective_message.reply_text("Valid command bhejo, example: /only")
                return True
            await self._update_perm_by_id(int(sdata.get("perm_id")), command=cmd)
            await self.clear_user_state(uid)
            await update.effective_message.reply_text("Command updated.")
            return True

        if state == STATE_NEW_BUTTON_NAME:
            request_id = sdata.get("request_id")
            preview = self.previews.get(request_id)
            if not preview or int(preview.user_id) != int(uid):
                await self.clear_user_state(uid)
                await update.effective_message.reply_text("Preview expired. Command dobara run karo.")
                return True
            if not text:
                await update.effective_message.reply_text("Valid folder name bhejo.")
                return True
            duplicate = await self._find_case_duplicate(text)
            if duplicate:
                await update.effective_message.reply_text(f"Duplicate folder exists: {duplicate.get('name')}")
                return True
            bid = await self._create_button_for_user(uid, text)
            await self.clear_user_state(uid)
            if not bid:
                await update.effective_message.reply_text("Folder create nahi ho paya.")
                return True
            fake_q = _MessageScreen(update.effective_message)
            await self._save_preview_to_button(fake_q, context, request_id, int(bid))
            return True

        return False

    async def handle_command(self, update, context) -> bool:
        if not update.effective_message or not update.effective_message.text:
            return False
        uid = update.effective_user.id
        if not await self.is_enabled():
            return False
        if not await self.is_authorized(uid):
            return False
        role = await self.get_user_role(uid)
        command_text = update.effective_message.text.strip()
        cmd = self._normalize_command(command_text)
        if not await self._is_command_allowed(uid, role, cmd):
            return False
        await self.start(context.bot)
        if not self.started:
            await update.effective_message.reply_text("Dark Mode Telethon env missing hai. Owner config check kare.")
            return True

        request_id = uuid.uuid4().hex
        req = DarkModeRequest(
            request_id=request_id,
            user_id=int(uid),
            chat_id=int(update.effective_chat.id),
            command_text=command_text,
            source_message_id=update.effective_message.message_id,
        )
        self.requests[request_id] = req
        await self._insert_request(req)
        await self.queue.put(req)
        try:
            await context.bot.delete_message(update.effective_chat.id, update.effective_message.message_id)
        except Exception as e:
            pass
        return True

    # Typing Indicator task loop
    async def _typing_loop(self, req_id: str, chat_id: int):
        while True:
            req = self.requests.get(req_id)
            if not req or req.status != "sent":
                break
            if self.bot:
                try: await self.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception: pass
            await asyncio.sleep(4)

    async def _queue_worker(self):
        while True:
            req = await self.queue.get()
            try:
                if self.bot:
                    try:
                        cmd_safe = html_lib.escape(req.command_text)
                        m = await self.bot.send_message(
                            req.chat_id,
                            f"⏳ <b>Processing your request</b>\n"
                            f"┌ Command: <code>{cmd_safe}</code>\n"
                            f"└ Status: <i>queued...</i>",
                            parse_mode="HTML",
                        )
                        self.temp_status_msgs[req.request_id] = m
                    except Exception: pass
                
                await self._send_to_private_group(req)
                await asyncio.sleep(self.queue_delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Dark Mode queue worker error: {e}")
            finally:
                self.queue.task_done()

    async def _send_to_private_group(self, req: DarkModeRequest):
        while True:
            try:
                sent = await self.client.send_message(self.private_group_id, req.command_text)
                req.telethon_message_id = int(sent.id)
                req.status = "sent"
                self.telethon_msg_to_request[int(sent.id)] = req.request_id
                await self._update_request(req.request_id, status="sent", telethon_message_id=int(sent.id))
                
                # Start typing indicator loop for the user waiting
                self.typing_tasks[req.request_id] = asyncio.create_task(self._typing_loop(req.request_id, req.chat_id))
                
                temp = self.temp_status_msgs.get(req.request_id)
                if temp and self.bot:
                    try:
                        await self.bot.edit_message_text(
                            "🔎 <b>Fetching your data</b>\n"
                            "└ Status: <i>almost ready...</i> ⏳",
                            chat_id=temp.chat.id, message_id=temp.message_id, parse_mode="HTML",
                        )
                    except Exception: pass

                return
            except Exception as e:
                if FloodWaitError is not None and isinstance(e, FloodWaitError):
                    await asyncio.sleep(int(getattr(e, "seconds", 1)) + 1)
                    continue
                raise

    async def _on_botb_new_message(self, event):
        msg = event.message
        req_id = self._request_id_from_botb_message(msg)
        if not req_id:
            return
        self.botb_msg_to_request[int(msg.id)] = req_id
        req = self.requests.get(req_id)
        if req:
            req.botb_message_id = int(msg.id)
            await self._update_request(req_id, status="botb_reply", botb_message_id=int(msg.id))
        await self._consider_final(req_id, msg, edited=False)

    async def _on_botb_message_edited(self, event):
        msg = event.message
        req_id = self.botb_msg_to_request.get(int(msg.id)) or self._request_id_from_botb_message(msg)
        if not req_id:
            return
        self.botb_msg_to_request[int(msg.id)] = req_id
        await self._consider_final(req_id, msg, edited=True)

    def _request_id_from_botb_message(self, msg) -> Optional[str]:
        reply_to = getattr(msg, "reply_to_msg_id", None)
        if not reply_to and getattr(msg, "reply_to", None):
            reply_to = getattr(msg.reply_to, "reply_to_msg_id", None)
        if reply_to:
            req_id = self.telethon_msg_to_request.get(int(reply_to))
            if req_id:
                return req_id
            req_id = self.botb_msg_to_request.get(int(reply_to))
            if req_id:
                return req_id
                
        if not reply_to:
            pending = [req for req in self.requests.values() if req.status not in ("delivered", "expired")]
            if len(pending) == 1:
                return pending[0].request_id
                
        return None

    async def _consider_final(self, req_id: str, msg, *, edited: bool):
        req = self.requests.get(req_id)
        if not req or req.status == "delivered":
            return
        if self._looks_interim(msg):
            await self._update_request(req_id, status="interim")
            
            temp = self.temp_status_msgs.get(req_id)
            if temp and self.bot:
                try:
                    await self.bot.edit_message_text(
                        "⚙️ <b>Working on it</b>\n"
                        "└ Status: <i>please wait...</i> ⏳",
                        chat_id=temp.chat.id, message_id=temp.message_id, parse_mode="HTML",
                    )
                except Exception: pass
            return
            
        old = self.final_tasks.pop(req_id, None)
        if old:
            old.cancel()
        if edited:
            await self._deliver_final(req_id, msg)
            return

        async def _settled_delivery():
            await asyncio.sleep(self.final_settle_seconds)
            await self._deliver_final(req_id, msg)

        self.final_tasks[req_id] = asyncio.create_task(_settled_delivery())

    def _looks_interim(self, msg) -> bool:
        if getattr(msg, "media", None):
            return False
        text = (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()
        low = text.casefold()
        if not text:
            return True
        if len(low) <= 180 and any(p in low for p in self.interim_patterns):
            return True
        if len(low) <= 80 and low.endswith(("...", ".")) and any(p in low for p in ("process", "wait", "load")):
            return True
        return False

    async def _deliver_final(self, req_id: str, msg):
        req = self.requests.get(req_id)
        if not req or req.status == "delivered":
            return
            
        old_typing = self.typing_tasks.pop(req_id, None)
        if old_typing and not old_typing.done():
            old_typing.cancel()
            
        temp = self.temp_status_msgs.pop(req_id, None)
        if temp and self.bot:
            try: await self.bot.delete_message(temp.chat.id, temp.message_id)
            except Exception: pass
            
        if self.bot is None:
            return
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to existing folder", callback_data=f"dm:add:{req_id}")],
            [InlineKeyboardButton("🆕 Create new folder", callback_data=f"dm:new:{req_id}")],
        ])
        text = (getattr(msg, "raw_text", None) or getattr(msg, "message", None) or "").strip()
        sent = None
        tmpdir = None
        try:
            if getattr(msg, "media", None):
                tmpdir = tempfile.mkdtemp(prefix="dark_mode_")
                path = await msg.download_media(file=tmpdir)
                if path:
                    caption = text[:1024] if text else None
                    with open(path, "rb") as fh:
                        sent = await self.bot.send_document(
                            chat_id=req.chat_id,
                            document=fh,
                            caption=caption,
                            reply_markup=markup,
                        )
                else:
                    sent = await self.bot.send_message(
                        chat_id=req.chat_id,
                        text=text or "Media could not be downloaded.",
                        reply_markup=markup,
                    )
            else:
                sent = await self.bot.send_message(
                    chat_id=req.chat_id,
                    text=text or "Final result empty hai.",
                    reply_markup=markup,
                )
        except Exception as e:
            logging.error(f"Dark mode deliver_final error: {e}")
            try:
                err_msg = await self.bot.send_message(req.chat_id, f"⚠️ Error sending result: {e}")
                async def _delete_err():
                    await asyncio.sleep(self.preview_seconds)
                    try:
                        await self.bot.delete_message(req.chat_id, err_msg.message_id)
                    except Exception:
                        pass
                asyncio.create_task(_delete_err())
            except Exception:
                pass
            finally:
                self._cleanup_request_tracking(req_id)
            return
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        if not sent:
            self._cleanup_request_tracking(req_id)
            return

        file_info = self._extract_file_info(sent)
        self.previews[req_id] = PreviewPayload(
            request_id=req_id,
            user_id=req.user_id,
            chat_id=req.chat_id,
            message_id=sent.message_id,
            file_info=file_info,
        )
        req.status = "delivered"
        await self._update_request(
            req_id,
            status="delivered",
            preview_chat_id=req.chat_id,
            preview_message_id=sent.message_id,
        )
        asyncio.create_task(self._expire_preview(req_id, req.chat_id, sent.message_id))

    def _extract_file_info(self, sent) -> Dict[str, Any]:
        if getattr(sent, "document", None):
            doc = sent.document
            name = (getattr(doc, "file_name", None) or "").lower()
            mime = (getattr(doc, "mime_type", None) or "").lower()
            ftype = "pdf" if mime == "application/pdf" or name.endswith(".pdf") else "document"
            return {
                "file_id": doc.file_id,
                "file_unique_id": doc.file_unique_id,
                "file_type": ftype,
                "caption": sent.caption or "",
            }
        if getattr(sent, "photo", None):
            p = sent.photo[-1]
            return {
                "file_id": p.file_id,
                "file_unique_id": p.file_unique_id,
                "file_type": "photo",
                "caption": sent.caption or "",
            }
        return {
            "file_id": f"text_{uuid.uuid4()}",
            "file_unique_id": f"textu_{uuid.uuid4()}",
            "file_type": "text",
            "caption": sent.text or "",
        }

    def _cleanup_request_tracking(self, request_id: str):
        req = self.requests.pop(request_id, None)
        if req:
            if req.telethon_message_id is not None:
                self.telethon_msg_to_request.pop(int(req.telethon_message_id), None)
            if req.botb_message_id is not None:
                self.botb_msg_to_request.pop(int(req.botb_message_id), None)
        old_task = self.final_tasks.pop(request_id, None)
        if old_task and not old_task.done():
            old_task.cancel()
            
        typing_task = self.typing_tasks.pop(request_id, None)
        if typing_task and not typing_task.done():
            typing_task.cancel()
            
        temp = self.temp_status_msgs.pop(request_id, None)
        if temp and self.bot:
            try: asyncio.create_task(self.bot.delete_message(temp.chat.id, temp.message_id))
            except Exception: pass

    async def _expire_preview(self, request_id: str, chat_id: int, message_id: int):
        await asyncio.sleep(self.preview_seconds)
        preview = self.previews.pop(request_id, None)
        if not preview:
            self._cleanup_request_tracking(request_id)
            return
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            pass
        await self._update_request(request_id, status="expired")
        self._cleanup_request_tracking(request_id)

    async def _show_panel(self, q, note: Optional[str] = None):
        enabled = await self.is_enabled()
        status_badge = "🟢 <b>ACTIVE</b>" if enabled else "🔴 <b>PAUSED</b>"
        bridge_badge = "🟢 READY" if self.started else "🟡 STANDBY"

        # Check for missing env config and build warning text
        missing_vars = []
        if TelegramClient is None:
            missing_vars.append("telethon package (not installed)")
        else:
            if not self._env_int("TELETHON_API_ID"):
                missing_vars.append("TELETHON_API_ID")
            if not os.getenv("TELETHON_API_HASH", "").strip():
                missing_vars.append("TELETHON_API_HASH")
            session_value = os.getenv("TELETHON_SESSION", "").strip()
            session_file = os.getenv("TELETHON_SESSION_FILE", "dark_mode_session").strip()
            if not session_value:
                session_path = session_file if session_file.endswith(".session") else f"{session_file}.session"
                if not os.path.exists(session_path):
                    missing_vars.append("TELETHON_SESSION (or session file)")
            if not self.private_group_id:
                missing_vars.append("DARK_MODE_PRIVATE_GROUP_ID")
            if not self.bot_b_id:
                missing_vars.append("DARK_MODE_BOT_B_ID")

        config_warning = ""
        if missing_vars:
            config_warning = "\n\n⚠️ <b>Missing Config:</b>\n" + "\n".join(f"  • <code>{v}</code>" for v in missing_vars)

        text = (
            "🌑 <b>Dark Mode — Control Center</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"Status  : {status_badge}\n"
            f"Bridge  : {bridge_badge}\n"
            "Access  : <i>Owner managed</i>"
            + config_warning
        )
        if note:
            text = f"✅ <i>{html_lib.escape(note)}</i>\n\n{text}"
        rows = [
            [InlineKeyboardButton("🔴 Disable Dark Mode" if enabled else "🟢 Enable Dark Mode", callback_data="dm:toggle")],
            [InlineKeyboardButton("🔐 Command Access Rules", callback_data="dm:perms")],
            [InlineKeyboardButton("↩️ Return to Admin Panel", callback_data="admin_panel")],
        ]
        await self._screen(q, text, InlineKeyboardMarkup(rows))

    async def _show_perms(self, q, note: Optional[str] = None):
        enabled = await self.is_enabled()
        cur = await self.db.aexecute(
            "SELECT id, command, scope, target_user_id FROM dark_mode_perms ORDER BY command COLLATE NOCASE, id DESC"
        )
        rows = cur.fetchall()
        status_badge = "🟢 ACTIVE" if enabled else "🔴 PAUSED"
        text = (
            "🔐 <b>Command Access Rules</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"Status      : {status_badge}\n"
            f"Saved rules : <b>{len(rows)}</b>"
        )
        if note:
            text = f"✅ <i>{html_lib.escape(note)}</i>\n\n{text}"
        if not rows:
            text += "\n\n<i>No command rules saved yet.</i>"
        kb = []
        for row in rows[:25]:
            pid, cmd, scope, target_uid = row
            icon = self._scope_icon(scope)
            label = f"{icon} {cmd} → {self._scope_label(scope, target_uid)}"
            kb.append([InlineKeyboardButton(label[:60], callback_data=f"dm:perm_view:{pid}")])
        kb.append([InlineKeyboardButton("🔴 Disable Dark Mode" if enabled else "🟢 Enable Dark Mode", callback_data="dm:toggle")])
        kb.append([InlineKeyboardButton("➕ Add New Rule", callback_data="dm:add_perm")])
        kb.append([InlineKeyboardButton("🌑 Dark Mode Center", callback_data="dm:panel")])
        await self._screen(q, text, InlineKeyboardMarkup(kb))

    async def _show_perm_detail(self, q, perm_id: int):
        cur = await self.db.aexecute(
            "SELECT command, scope, target_user_id FROM dark_mode_perms WHERE id = ?",
            (perm_id,),
        )
        row = cur.fetchone()
        if not row:
            await self._show_perms(q, "Permission not found.")
            return
        cmd, scope, target_uid = row
        icon = self._scope_icon(scope)
        text = (
            "📋 <b>Rule Detail</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"Command : <code>{html_lib.escape(cmd)}</code>\n"
            f"Access  : {icon} {html_lib.escape(self._scope_label(scope, target_uid))}"
        )
        kb = [
            [InlineKeyboardButton("✏️ Edit Command", callback_data=f"dm:perm_edit_cmd:{perm_id}")],
            [InlineKeyboardButton("🎯 Edit Access Target", callback_data=f"dm:perm_edit_scope:{perm_id}")],
            [InlineKeyboardButton("🗑️ Delete Rule", callback_data=f"dm:perm_delete:{perm_id}")],
            [InlineKeyboardButton("🔐 Access Rules", callback_data="dm:perms")],
        ]
        await self._screen(q, text, InlineKeyboardMarkup(kb))

    async def _show_edit_scope(self, q, perm_id: int):
        text = "🎯 <b>Select Access Target</b>"
        kb = [
            [InlineKeyboardButton("👑 Co-Owners", callback_data=f"dm:perm_scope_edit:{perm_id}:{SCOPE_CO_OWNERS}")],
            [InlineKeyboardButton("👥 All UAdmins", callback_data=f"dm:perm_scope_edit:{perm_id}:{SCOPE_ALL_UADMINS}")],
            [InlineKeyboardButton("🎯 Specific UAdmin", callback_data=f"dm:perm_scope_edit:{perm_id}:{SCOPE_SPECIFIC_UADMIN}")],
            [InlineKeyboardButton("📋 Rule Detail", callback_data=f"dm:perm_view:{perm_id}")],
        ]
        await self._screen(q, text, InlineKeyboardMarkup(kb))

    async def _save_perm_scope_from_state(self, q, uid: int, scope: str):
        if scope == SCOPE_SPECIFIC_UADMIN:
            uadmins = await self.get_all_user_admins()
            kb = []
            for ua in uadmins[:50]:
                target = int(ua["user_id"])
                label = f"{ua.get('nickname') or 'UAdmin'} (ID:{target})"
                kb.append([InlineKeyboardButton(label[:60], callback_data=f"dm:perm_uadmin:{target}")])
            kb.append([InlineKeyboardButton("Access Rules", callback_data="dm:perms")])
            await self._screen(q, "Select Specific UAdmin", InlineKeyboardMarkup(kb))
            return
        await self._save_perm_from_state(q, uid, scope, None)

    async def _save_perm_from_state(self, q, uid: int, scope: str, target_uid: Optional[int]):
        cur = await self.db.aexecute("SELECT data FROM user_states WHERE user_id = ?", (int(uid),))
        row = cur.fetchone()
        data = {}
        if row and row[0]:
            import json
            data = json.loads(row[0])
        cmd = data.get("command")
        if not cmd:
            await self._show_perms(q, "Command state expired.")
            return
        try:
            await self.db.aexecute(
                "INSERT OR REPLACE INTO dark_mode_perms (command, scope, target_user_id, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (cmd, scope, target_uid, int(uid), self._now(), self._now()),
            )
            await self.clear_user_state(uid)
            self._invalidate_perm_cache()
            await self._show_perms(q, "Permission saved.")
        except Exception as e:
            await self._show_perms(q, f"Save failed: {e}")

    async def _update_perm_scope(self, q, perm_id: int, scope: str):
        if scope == SCOPE_SPECIFIC_UADMIN:
            uadmins = await self.get_all_user_admins()
            kb = []
            for ua in uadmins[:50]:
                target = int(ua["user_id"])
                label = f"{ua.get('nickname') or 'UAdmin'} (ID:{target})"
                kb.append([InlineKeyboardButton(label[:60], callback_data=f"dm:perm_uadmin_edit:{perm_id}:{target}")])
            kb.append([InlineKeyboardButton("Rule Detail", callback_data=f"dm:perm_view:{perm_id}")])
            await self._screen(q, "Select Specific UAdmin", InlineKeyboardMarkup(kb))
            return
        await self._update_perm(q, perm_id, None, scope, None)

    async def _update_perm(self, q, perm_id: int, command: Optional[str], scope: Optional[str], target_uid: Optional[int]):
        await self._update_perm_by_id(perm_id, command=command, scope=scope, target_uid=target_uid)
        await self._show_perm_detail(q, perm_id)

    async def _update_perm_by_id(
        self,
        perm_id: int,
        *,
        command: Optional[str] = None,
        scope: Optional[str] = None,
        target_uid: Optional[int] = None,
    ):
        parts = []
        params = []
        if command is not None:
            parts.append("command = ?")
            params.append(command)
        if scope is not None:
            parts.append("scope = ?")
            params.append(scope)
            parts.append("target_user_id = ?")
            params.append(target_uid)
        parts.append("updated_at = ?")
        params.append(self._now())
        params.append(int(perm_id))
        await self.db.aexecute(f"UPDATE dark_mode_perms SET {', '.join(parts)} WHERE id = ?", tuple(params))
        self._invalidate_perm_cache()

    def _scope_markup(self, prefix: str):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Co-Owners", callback_data=f"{prefix}{SCOPE_CO_OWNERS}")],
            [InlineKeyboardButton("👥 All UAdmins", callback_data=f"{prefix}{SCOPE_ALL_UADMINS}")],
            [InlineKeyboardButton("🎯 Specific UAdmin", callback_data=f"{prefix}{SCOPE_SPECIFIC_UADMIN}")],
            [InlineKeyboardButton("🔐 Access Rules", callback_data="dm:perms")],
        ])

    def _scope_icon(self, scope: str) -> str:
        return {
            SCOPE_CO_OWNERS: "👑",
            SCOPE_ALL_UADMINS: "👥",
            SCOPE_SPECIFIC_UADMIN: "🎯",
        }.get(scope, "•")

    def _scope_label(self, scope: str, target_uid: Optional[int]) -> str:
        if scope == SCOPE_CO_OWNERS:
            return "Co-Owners"
        if scope == SCOPE_ALL_UADMINS:
            return "All UAdmins"
        if scope == SCOPE_SPECIFIC_UADMIN:
            return f"UAdmin {target_uid}"
        return scope or "-"

    async def _get_perms_cached(self) -> list:
        now = time.time()
        if self._perm_cache is not None and (now - self._perm_cache_ts) < self._perm_cache_ttl:
            return self._perm_cache
        cur = await self.db.aexecute("SELECT command, scope, target_user_id FROM dark_mode_perms")
        self._perm_cache = cur.fetchall()
        self._perm_cache_ts = now
        return self._perm_cache

    def _invalidate_perm_cache(self):
        self._perm_cache = None
        self._perm_cache_ts = 0

    async def _is_command_allowed(self, uid: int, role: str, cmd: str) -> bool:
        if self._is_owner(uid):
            return True
        rows = await self._get_perms_cached()
        for command, scope, target_uid in rows:
            if command != cmd:
                continue
            if role == "co_admin" and scope == SCOPE_CO_OWNERS:
                return True
            if role == "user_admin" and scope == SCOPE_ALL_UADMINS:
                return True
            if role == "user_admin" and scope == SCOPE_SPECIFIC_UADMIN and int(target_uid or 0) == int(uid):
                return True
        return False

    async def _show_existing_buttons(self, q, update, context, request_id: str):
        if not await self._validate_preview_owner(q, request_id):
            return
        uid = update.effective_user.id
        btns = await self.get_manage_buttons_for_user(uid)
        if not btns:
            await self._screen(q, "📭 <b>Koi manageable folder nahi mila.</b>\nNaya folder banao:", InlineKeyboardMarkup([
                [InlineKeyboardButton("🆕 Create new folder", callback_data=f"dm:new:{request_id}")],
            ]))
            return
        kb = []
        for b in btns[:30]:
            kb.append([InlineKeyboardButton("🔘 " + (b.get("name") or f"Folder {b.get('id')}")[:53], callback_data=f"dm:pick:{request_id}:{b['id']}")])
        kb.append([InlineKeyboardButton("↩️ Back", callback_data=f"dm:new:{request_id}")])
        await self._screen(q, "📌 <b>Select a folder:</b>", InlineKeyboardMarkup(kb))

    async def _validate_preview_owner(self, q, request_id: str) -> bool:
        preview = self.previews.get(request_id)
        if not preview:
            await self._screen(q, "⌛ Preview expired. Command dobara run karo.")
            return False
        if int(q.from_user.id) != int(preview.user_id):
            await self._screen(q, "🚫 Ye preview kisi aur user ka hai.")
            return False
        return True

    async def _find_case_duplicate(self, name: str) -> Optional[dict]:
        target = (name or "").strip().casefold()
        for btn in await self.get_all_buttons_cached(force=True):
            if (btn.get("name") or "").strip().casefold() == target:
                return btn
        return None

    async def _create_button_for_user(self, uid: int, name: str) -> Optional[int]:
        role = await self.get_user_role(uid)
        if role == "user_admin":
            await self.db.aexecute(
                "INSERT INTO buttons (name, visibility, btn_type, created_by, visible_to_user_id) VALUES (?, 'specific_uadmin', 'callback', ?, ?)",
                (name, int(uid), int(uid)),
            )
        elif role == "co_admin":
            await self.db.aexecute(
                "INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?, 'coowner_owner', 'callback', ?)",
                (name, int(uid)),
            )
        else:
            await self.db.aexecute(
                "INSERT INTO buttons (name, visibility, btn_type, created_by) VALUES (?, 'owner_only', 'callback', ?)",
                (name, int(uid)),
            )
        self.invalidate_button_cache()
        duplicate = await self._find_case_duplicate(name)
        return int(duplicate["id"]) if duplicate else None

    async def _save_preview_to_button(self, q, context, request_id: str, bid: int):
        if not await self._validate_preview_owner(q, request_id):
            return
        preview = self.previews.get(request_id)
        btn = await self.get_button_by_id(bid)
        if not btn:
            await self._screen(q, "❌ Folder not found.")
            return
        role = await self.get_user_role(preview.user_id)
        if not await self.can_add_files_to_button(preview.user_id, btn, role):
            await self._screen(q, "🚫 Is folder me add karne ki permission nahi hai.")
            return

        file_info = dict(preview.file_info)
        backup_chat = None
        backup_mid = None
        if self.backup_channel_id and file_info["file_type"] != "text":
            try:
                backup_caption = self.backup_caption_with_button(file_info.get("caption") or "", btn.get("name") or "")
                backup_mid = await self._send_backup_copy(context, file_info, backup_caption)
                backup_chat = int(self.backup_channel_id) if backup_mid else None
            except Exception as e:
                logging.error(f"Dark Mode backup failed: {e}")

        await self.db.aexecute(
            "INSERT INTO button_files (button_id, file_id, file_unique_id, file_type, caption, backup_chat_id, backup_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                int(bid),
                file_info["file_id"],
                file_info["file_unique_id"],
                file_info["file_type"],
                file_info.get("caption") or "",
                backup_chat,
                backup_mid,
                self._now(),
            ),
        )
        self.invalidate_button_cache()
        if file_info["file_type"] in self.pdf_merge_types:
            if self.schedule_pdf_rebuild:
                self.schedule_pdf_rebuild(context, int(bid), btn)
            else:
                await self.refresh_button_pdf_backup(context, int(bid), btn)
        await self._delete_preview(request_id)
        safe_name = html_lib.escape(btn.get("name") or "")
        await self._screen(q, f"✅ <b>Saved</b> → {safe_name}")

    async def _send_backup_copy(self, context, file_info: dict, caption: str) -> Optional[int]:
        chat_id = int(self.backup_channel_id)
        ftype = file_info["file_type"]
        if ftype == "photo":
            msg = await context.bot.send_photo(chat_id, photo=file_info["file_id"], caption=caption)
        elif ftype == "video":
            msg = await context.bot.send_video(chat_id, video=file_info["file_id"], caption=caption)
        elif ftype == "audio":
            msg = await context.bot.send_audio(chat_id, audio=file_info["file_id"], caption=caption)
        elif ftype == "voice":
            msg = await context.bot.send_voice(chat_id, voice=file_info["file_id"], caption=caption)
        else:
            msg = await context.bot.send_document(chat_id, document=file_info["file_id"], caption=caption)
        return msg.message_id

    async def _delete_preview(self, request_id: str):
        preview = self.previews.pop(request_id, None)
        self._cleanup_request_tracking(request_id)
        if not preview or self.bot is None:
            return
        try:
            await self.bot.delete_message(preview.chat_id, preview.message_id)
        except Exception as e:
            pass

    async def _insert_request(self, req: DarkModeRequest):
        await self.db.aexecute(
            "INSERT OR REPLACE INTO dark_mode_requests (request_id, source_user_id, source_chat_id, source_message_id, command_text, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (req.request_id, req.user_id, req.chat_id, req.source_message_id, req.command_text, req.status, self._now(), self._now()),
        )

    async def _update_request(self, request_id: str, **fields):
        if not fields:
            return
        allowed = {
            "telethon_message_id",
            "botb_message_id",
            "status",
            "preview_chat_id",
            "preview_message_id",
        }
        parts = []
        params = []
        for key, value in fields.items():
            if key in allowed:
                parts.append(f"{key} = ?")
                params.append(value)
        if not parts:
            return
        parts.append("updated_at = ?")
        params.append(self._now())
        params.append(request_id)
        await self.db.aexecute(f"UPDATE dark_mode_requests SET {', '.join(parts)} WHERE request_id = ?", tuple(params))

    async def _screen(self, q, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        try:
            await q.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception as e:
            if "message is not modified" in str(e).casefold():
                return
            try:
                await q.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
            except Exception as e2:
                pass


class _MessageScreen:
    def __init__(self, message):
        self.message = message
        self.from_user = message.from_user

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

