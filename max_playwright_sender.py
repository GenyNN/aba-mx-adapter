import asyncio
import base64
from collections import OrderedDict
import json
import logging
import os
import random
import re
import struct
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# Structured JSON Logger setup
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)

logger = logging.getLogger("max_worker")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)

DEFAULT_SESSION_FILE = "max_auth.json"
DEFAULT_BASE_URL = "https://web.max.ru"
DEFAULT_RESTART_INTERVAL_SECONDS = 60 * 60
_NOTIFICATION_PREFIX = "🔔 Получены новые сообщения:"
_PHONE_CHAT_CACHE_MAX_SIZE = 5000
_DIAG_DUMP_MAX_FILES = 20
_DIAG_DUMP_MAX_AGE = timedelta(hours=24)

class MaxMessengerError(RuntimeError):
    """Base error for Max messenger automation."""

class ContactNotFoundError(MaxMessengerError):
    """Raised when the phone number cannot be found."""

@dataclass(frozen=True)
class SendMaxMessageResult:
    sent_ok: bool
    status_note: str
    error_message: str = ""
    chat_id: Optional[str] = None

@dataclass(frozen=True)
class CheckMaxMessageResult:
    reply_value: str
    check_ok: bool = True
    replied_at: Optional[str] = None
    is_viewed: bool = False

# --- Selectors ---
_SEARCH_PLUS_BUTTON_SELECTORS = ["button:has(use[href='#icon_plus'])"]
_FIND_BY_NUMBER_ITEM_MENU_SELECTORS = ["[role='menuitem']:has-text('Найти по номеру')", "[role='menuitem']:has-text('Search by number')"]
_CONTACT_NUMBER_INPUT_SELECTORS = ["form[id='findContact'] input"]
_FIND_CONTACT_SUBMIT_SELECTORS = [
    "button[type='submit'][form='findContact']",
    "form#findContact ~ * button[type='submit']",
    "form#findContact button[type='submit']",
]
_MESSAGE_INPUT_SELECTORS = [
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][data-testid*='composer']",
    "textarea[placeholder*='Сообщение']",
    "textarea[placeholder*='Message']",
    "div[placeholder*='Message']"
]
_ATTACH_BUTTON_SELECTORS = [
    "button:has(use[href*='paperclip'])",
    "button:has(use[href*='attach'])",
    "button[aria-label*='Прикреп']",
    "button[aria-label*='Attach']",
]
_ATTACH_MENU_MEDIA_SELECTORS = [
    "[role='menuitem']:has-text('Фото или видео')",
    "[role='menuitem']:has-text('Фото и видео')",
    "[role='menuitem']:has-text('Фото/Видео')",
    "[role='menuitem']:has-text('Фото')",
    "[role='menuitem']:has-text('Photo or video')",
    "[role='menuitem']:has-text('Photo & video')",
    "[role='menuitem']:has-text('Photo/Video')",
    "[role='menuitem']:has-text('Photo')",
]
_ATTACH_MENU_FILE_SELECTORS = [
    "[role='menuitem']:has-text('Файл')",
    "[role='menuitem']:has-text('Документ')",
    "[role='menuitem']:has-text('File')",
    "[role='menuitem']:has-text('Document')",
]
_ATTACHMENT_PREVIEW_SELECTORS = [
    "[class*='attachment']",
    "[data-testid*='attachment']",
    "a[href][download]",
]

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif", ".tiff", ".tif"}


def _is_image_file(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in _IMAGE_EXTENSIONS
#"div[placeholder*='Messagne'
_OPENED_CHAT_SELECTORS = [".openedChat", "[class*='openedChat']"]
_CHAT_HISTORY_SELECTORS = [".openedChat .history", "[class*='openedChat'] [class*='history']"]
_BACK_BUTTON_SELECTORS = ["button.backBtn", "button:has(use[href='#icon_arrow_left'])"]
_USER_NOT_FOUND_SELECTORS = [
    "text=Пользователь не найден",
    "text=пользователь не найден",
    "text=Никого не найдено",
    "text=User not found",
    "text=No users found",
]

_PARSE_LAST_MESSAGE_JS = """
() => {
  const chat = document.querySelector('.openedChat') || document.querySelector('[class*="openedChat"]');
  if (!chat) return { error: 'no_chat' };
  const history = chat.querySelector('.history') || chat.querySelector('[class*="history"]');
  if (!history) return { error: 'no_history' };
  const wrappers = [...history.querySelectorAll('div[class*="messageWrapper"]')];
  if (!wrappers.length) return { error: 'no_messages' };
  const last = wrappers[wrappers.length - 1];
  const isOut = (last.className || '').includes('messageWrapper--isOut');
  const bubbleRoot = last.querySelector('[data-bubbles-variant]');
  const variant = bubbleRoot ? bubbleRoot.getAttribute('data-bubbles-variant') : null;
  const statusUse = last.querySelector('.indicators use');
  const statusIcon = statusUse ? (statusUse.getAttribute('href') || '') : '';
  const textEl = last.querySelector('.bubble span.text') || last.querySelector('.bubble .text');
  let text = '';
  if (textEl) {
    text = (textEl.textContent || '').replace(/\\s+/g, ' ').trim();
  }
  // Try to find time
  const timeEl = last.querySelector('.time') || last.querySelector('[class*="time"]');
  const timeText = timeEl ? timeEl.textContent : null;
  
  return { isOut, variant, statusIcon, text, timeText };
}
"""

class MaxBrowserManager:
    def __init__(
        self,
        session_file: str = DEFAULT_SESSION_FILE,
        headless: bool = True,
        max_tasks: int = 50,
        restart_interval_seconds: int = DEFAULT_RESTART_INTERVAL_SECONDS,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.session_file = session_file
        self.headless = headless
        self.max_tasks = max_tasks
        self.restart_interval_seconds = restart_interval_seconds
        self.base_url = base_url
        self.tasks_count = 0
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._restart_task: Optional[asyncio.Task] = None
        self._listener_context: Optional[BrowserContext] = None
        self._listener_page: Optional[Page] = None
        self._ws_actions: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._viewer_id: Optional[int] = self._load_viewer_id()
        self._chat_by_phone: "OrderedDict[str, str]" = OrderedDict()
        self._phone_by_chat_id: "OrderedDict[str, str]" = OrderedDict()
        self._chat_status: Dict[str, Dict[str, Any]] = {}

    async def start(self):
        if not self.browser:
            logger.info("Starting Chromium browser...")
            await self._start_browser_only()
            self.tasks_count = 0
        if not self._listener_page:
            await self._start_listener()
        if not self._restart_task:
            self._restart_task = asyncio.create_task(self._scheduled_restart_loop())

    async def stop(self):
        if self._restart_task:
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
            self._restart_task = None
        await self._close_listener()
        await self._close_browser_only()

    async def _start_browser_only(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            channel="chrome",
        )

    async def _close_browser_only(self):
        if self.browser:
            logger.info("Closing Chromium browser...")
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def _scheduled_restart_loop(self):
        try:
            while True:
                await asyncio.sleep(self.restart_interval_seconds)
                async with self._lock:
                    logger.info("Scheduled browser restart triggered")
                    await self._restart_browser("scheduled")
        except asyncio.CancelledError:
            return

    def _load_viewer_id(self) -> Optional[int]:
        try:
            session_path = Path(self.session_file)
            if not session_path.exists():
                return None
            data = json.loads(session_path.read_text(encoding="utf-8"))
            for origin in data.get("origins", []):
                for kv in origin.get("localStorage", []):
                    if kv.get("name") == "__oneme_auth":
                        auth = json.loads(kv.get("value") or "{}")
                        viewer_id = auth.get("viewerId")
                        return int(viewer_id) if viewer_id is not None else None
        except Exception:
            return None
        return None

    async def _restart_browser(self, reason: str):
        logger.info(f"Restarting browser. reason={reason}")
        self._chat_status.clear()
        await self._close_listener()
        await self._close_browser_only()
        await self._start_browser_only()
        self.tasks_count = 0
        await self._start_listener()

    async def _close_listener(self):
        if self._listener_page:
            try:
                await self._listener_page.close()
            except Exception:
                pass
            self._listener_page = None
        if self._listener_context:
            try:
                await self._listener_context.close()
            except Exception:
                pass
            self._listener_context = None

    async def get_context(self) -> BrowserContext:
        async with self._lock:
            self.tasks_count += 1
            if self.tasks_count > self.max_tasks:
                logger.info(f"Task limit ({self.max_tasks}) reached. Restarting browser...")
                await self._restart_browser("task_limit")
            
            if not self.browser:
                await self.start()
                
            session_path = Path(self.session_file)
            if not session_path.exists():
                raise MaxMessengerError(f"Session file not found: {session_path}")
                
            context = await self.browser.new_context(storage_state=str(session_path))
            await self._block_heavy_resources(context)
            return context

    async def _start_listener(self):
        if not self.browser:
            await self._start_browser_only()
        session_path = Path(self.session_file)
        if not session_path.exists():
            raise MaxMessengerError(f"Session file not found: {session_path}")
        context = await self.browser.new_context(storage_state=str(session_path))
        await self._block_heavy_resources(context)
        page = await context.new_page()
        await page.expose_function("__max_ws_sniffer_emit", self._on_ws_sniffer_emit)
        sniffer_path = Path(__file__).with_name("max_ws_sniffer.js")
        if sniffer_path.exists():
            await page.add_init_script(path=str(sniffer_path))
        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=45000)
        self._listener_context = context
        self._listener_page = page

    async def _attach_ws_sniffer_to_page(self, page: Page, phone_for_mapping: Optional[str]) -> None:
        sniffer_path = Path(__file__).with_name("max_ws_sniffer.js")
        if sniffer_path.exists():
            await page.add_init_script(path=str(sniffer_path))

        if not phone_for_mapping:
            await page.expose_function("__max_ws_sniffer_emit", self._on_ws_sniffer_emit)
            return

        async def _emit(data: Dict[str, Any]):
            await self._on_ws_sniffer_emit(data)
            try:
                if not isinstance(data, dict) or data.get("type") != "frame":
                    return
                opcode = int(data.get("opcode") or 0)
                if opcode != 2:
                    return
                payload_b64 = data.get("payload")
                if not isinstance(payload_b64, str):
                    return
                raw = base64.b64decode(payload_b64)
                decoded = _decode_max_binary_frame(raw)
                if not decoded:
                    return
                payload = decoded.get("payload")
                if not isinstance(payload, dict):
                    return
                chat_id = payload.get("chatId")
                if chat_id is None:
                    return
                chat_id_str = str(chat_id)
                if phone_for_mapping and chat_id_str and phone_for_mapping not in self._chat_by_phone:
                    self._cache_phone_chat(phone=phone_for_mapping, chat_id=chat_id_str)
            except Exception:
                return

        await page.expose_function("__max_ws_sniffer_emit", _emit)

    async def _on_ws_sniffer_emit(self, data: Dict[str, Any]):
        try:
            if not isinstance(data, dict):
                return
            if data.get("type") != "frame":
                return
            opcode = int(data.get("opcode") or 0)
            direction = str(data.get("dir") or "")
            if opcode == 1:
                return
            if opcode != 2:
                return
            payload_b64 = data.get("payload")
            if not isinstance(payload_b64, str):
                return
            raw = base64.b64decode(payload_b64)
            decoded = _decode_max_binary_frame(raw)
            if not decoded:
                return
            max_opcode = decoded.get("opcode")
            if max_opcode in (128, 130, 50):
                await self._handle_max_protocol_event(direction, decoded)
        except Exception as e:
            logger.error(f"WS sniffer emit handler failed: {e}")

    async def _push_ws_action(self, event: Dict[str, Any]):
        try:
            self._ws_actions.put_nowait(event)
        except asyncio.QueueFull:
            try:
                _ = self._ws_actions.get_nowait()
            except Exception:
                return
            try:
                self._ws_actions.put_nowait(event)
            except Exception:
                return

    async def _handle_max_protocol_event(self, direction: str, decoded: Dict[str, Any]):
        opcode = decoded.get("opcode")
        payload = decoded.get("payload")
        if not isinstance(payload, dict):
            return

        if opcode == 128:
            chat_id = payload.get("chatId")
            message = payload.get("message")
            if chat_id is None or not isinstance(message, dict):
                return
            chat_id_str = str(chat_id)

            msg_text = _extract_message_text(message)
            msg_time_iso = _extract_message_time_iso(message)
            is_out = _extract_message_is_out(message, viewer_id=self._viewer_id)
            reply_to = message.get("replyTo")

            st = self._chat_status.setdefault(chat_id_str, {})
            if not is_out:
                if isinstance(msg_text, str) and msg_text.startswith(_NOTIFICATION_PREFIX):
                    return
                prev_text = st.get("last_incoming_text")
                prev_time = st.get("last_incoming_time")
                if prev_text == msg_text and prev_time == msg_time_iso:
                    return
                st["last_incoming_text"] = msg_text
                st["last_incoming_time"] = msg_time_iso
                st["last_incoming_is_reply"] = reply_to is not None
                phone = self._phone_by_chat_id.get(chat_id_str)
                logger.info(
                    json.dumps(
                        {
                            "event": "max_ws_incoming_message",
                            "phone": phone,
                            "chatId": chat_id_str,
                            "text": msg_text,
                            "isReply": reply_to is not None,
                            "time": msg_time_iso,
                        },
                        ensure_ascii=False,
                    )
                )
                await self._push_ws_action(
                    {
                        "status": "replied",
                        "chat_id": chat_id_str,
                        "reply_text": msg_text,
                        "timestamp": msg_time_iso or datetime.utcnow().isoformat() + "Z",
                    }
                )
            else:
                st["last_outgoing_text"] = msg_text
                st["last_outgoing_time"] = msg_time_iso
        elif opcode == 130:
            chat_id = payload.get("chatId")
            user_id = payload.get("userId")
            if chat_id is None or user_id is None:
                return
            chat_id_str = str(chat_id)
            try:
                user_id_int = int(user_id)
            except Exception:
                user_id_int = None
            if self._viewer_id is not None and user_id_int == self._viewer_id:
                return
            st = self._chat_status.setdefault(chat_id_str, {})
            mark = payload.get("mark")
            try:
                mark_int = int(mark) if mark is not None else None
            except Exception:
                mark_int = None
            prev_mark = st.get("last_view_mark")
            try:
                prev_mark_int = int(prev_mark) if prev_mark is not None else None
            except Exception:
                prev_mark_int = None
            if mark_int is not None and prev_mark_int is not None and mark_int <= prev_mark_int:
                return
            if mark_int is not None:
                st["last_view_mark"] = mark_int
            st["is_viewed"] = True
            st["viewed_at"] = datetime.utcnow().isoformat() + "Z"
            phone = self._phone_by_chat_id.get(chat_id_str)
            logger.info(
                json.dumps(
                    {
                        "event": "max_ws_read_receipt",
                        "phone": phone,
                        "chatId": chat_id_str,
                        "userId": user_id,
                        "mark": payload.get("mark"),
                        "unread": payload.get("unread"),
                        "ts": st["viewed_at"],
                    },
                    ensure_ascii=False,
                )
            )
            await self._push_ws_action(
                {
                    "status": "viewed",
                    "chat_id": chat_id_str,
                    "timestamp": st["viewed_at"],
                }
            )

    async def _block_heavy_resources(self, context: BrowserContext) -> None:
        blocked_resource_types = {"media", "font"}
        blocked_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
        allowed_image_hosts = {"web.max.ru", "st.max.ru"}

        async def route_handler(route):
            request = route.request
            url = request.url.lower()
            # Allow inline assets (emojis, sprites) served from MAX CDN
            try:
                host = request.url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
                if request.resource_type == "image" and host in allowed_image_hosts:
                    await route.continue_()
                    return
            except Exception:
                pass
            if request.resource_type in blocked_resource_types:
                await route.abort()
                return
            if request.resource_type == "image":
                # Still block third-party images (ads, tracking, heavy avatars from outside MAX)
                await route.abort()
                return
            if any(ext in url for ext in blocked_extensions):
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)

    async def _wait_and_get_first(self, page: Page, selectors: Iterable[str], timeout_ms: int = 7000) -> Optional[str]:
        for sel in selectors:
            selector = sel
            # Playwright wait_for_selector accepts CSS by default. Auto-prefix xpath= for
            # XPath-style selectors (//...) so callers don't need to remember to add it.
            if selector.startswith("//") or selector.startswith(".//"):
                selector = "xpath=" + selector
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return sel
            except PlaywrightTimeoutError:
                continue
        return None

    async def _diag_snapshot(self, page: Page, prefix: str) -> None:
        """Best-effort diagnostic snapshot for debugging selector wait failures."""
        try:
            url = page.url
            title = await page.title()
            buttons = await page.locator("button").count()
            uses = await page.locator("use").count()
            hrefs = await page.evaluate(
                "() => [...new Set([...document.querySelectorAll('use')].map(u => u.getAttribute('href') || u.getAttribute('xlink:href') || ''))].slice(0, 20)"
            )
            ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            screenshot_path = f"/tmp/diag_{prefix}_{ts}.png"
            html_path = f"/tmp/diag_{prefix}_{ts}.html"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except Exception as se:
                screenshot_path = f"<screenshot-failed:{se}>"
            try:
                Path(html_path).write_text(await page.content(), encoding="utf-8")
            except Exception as he:
                html_path = f"<html-failed:{he}>"
            logger.error(
                f"[DIAG:{prefix}] url={url} title={title!r} buttons={buttons} uses={uses} hrefs={hrefs} "
                f"screenshot={screenshot_path} html={html_path}"
            )
            self._cleanup_diag_dumps(prefix=prefix)
        except Exception as e:
            logger.exception(f"[DIAG:{prefix}] snapshot collection failed: {e}")

    def _cleanup_diag_dumps(self, prefix: str) -> None:
        try:
            now = datetime.utcnow()
            candidates = list(Path("/tmp").glob("diag_*.*"))
            keep: List[Path] = []
            for p in candidates:
                try:
                    mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                except Exception:
                    continue
                if now - mtime <= _DIAG_DUMP_MAX_AGE:
                    keep.append(p)
            keep.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            to_delete = keep[_DIAG_DUMP_MAX_FILES:]
            old_to_delete = [p for p in candidates if p not in keep]
            for p in to_delete + old_to_delete:
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            return

    async def _open_chat_by_phone(self, page: Page, phone: str) -> bool:
        logger.info(f"Opening chat for phone: {phone}")
        # Return to chat list if needed
        if await page.locator(".openedChat, [class*='openedChat']").count() > 0:
            back_selector = await self._wait_and_get_first(page, _BACK_BUTTON_SELECTORS, timeout_ms=2000)
            if back_selector:
                await page.click(back_selector)
                await self._wait_and_get_first(page, _SEARCH_PLUS_BUTTON_SELECTORS, timeout_ms=2000)

        open_selector = await self._wait_and_get_first(page, _SEARCH_PLUS_BUTTON_SELECTORS, timeout_ms=10000)
        if not open_selector:
            await self._diag_snapshot(page, "plus_button_missing")
            raise MaxMessengerError("Button 'Начать общение' not found.")
        await page.click(open_selector)

        find_by_number_selector = await self._wait_and_get_first(page, _FIND_BY_NUMBER_ITEM_MENU_SELECTORS, timeout_ms=5000)
        if not find_by_number_selector:
            raise MaxMessengerError("Menu item 'Найти по номеру' not found.")
        await page.click(find_by_number_selector)

        phone_input_selector = await self._wait_and_get_first(page, _CONTACT_NUMBER_INPUT_SELECTORS, timeout_ms=8000)
        if not phone_input_selector:
            raise MaxMessengerError("Phone input not found.")

        await page.fill(phone_input_selector, phone)

        submit_selector = await self._wait_and_get_first(page, _FIND_CONTACT_SUBMIT_SELECTORS, timeout_ms=8000)
        if not submit_selector:
            raise MaxMessengerError("Submit button 'Найти в MAX' not found.")
        await page.click(submit_selector)

        not_found_selector = await self._wait_and_get_first(page, _USER_NOT_FOUND_SELECTORS, timeout_ms=2500)
        if not_found_selector:
            raise ContactNotFoundError(f"User not found by phone: {phone}")

        # Auto-wait for chat to open (no arbitrary sleep buffers)
        chat_found = await self._wait_and_get_first(page, _OPENED_CHAT_SELECTORS + _MESSAGE_INPUT_SELECTORS, timeout_ms=10000)
        if not chat_found:
            raise ContactNotFoundError(f"User not found by phone: {phone}")
        return True

    async def _open_chat_by_chat_id(self, page: Page, chat_id: str, base_url: str) -> bool:
        logger.info(f"Opening existing chat by chat_id: {chat_id}")
        candidates = [
            f"{base_url}/{chat_id}",
        ]
        #f"{base_url}?chatId={chat_id}",
        #f"{base_url}/?chatId={chat_id}",
        #f"{base_url}/#/chats/{chat_id}",

        for url in candidates:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                opened = await self._wait_and_get_first(page, _OPENED_CHAT_SELECTORS + _MESSAGE_INPUT_SELECTORS, timeout_ms=4000)
                if opened:
                    return True
            except Exception:
                continue

        await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
        loc = page.locator(
            f"[data-chat-id='{chat_id}'], [data-chatid='{chat_id}'], a[href*='{chat_id}']"
        )
        if await loc.count() > 0:
            await loc.first.click()
            opened = await self._wait_and_get_first(page, _OPENED_CHAT_SELECTORS + _MESSAGE_INPUT_SELECTORS, timeout_ms=8000)
            return bool(opened)
        return False

    async def _type_like_human(self, page: Page, selector: str, text: str) -> None:
        await page.click(selector)
        delay_ms = random.randint(25, 90)
        await page.keyboard.type(text, delay=delay_ms)

    async def _attach_file_to_chat(self, page: Page, attachment_path: Union[str, Path]) -> None:
        path = Path(attachment_path)
        if not path.exists():
            raise MaxMessengerError("Attachment file not found")

        attach_selector = await self._wait_and_get_first(page, _ATTACH_BUTTON_SELECTORS, timeout_ms=8000)
        if not attach_selector:
            await self._diag_snapshot(page, "attach_button_missing")
            raise MaxMessengerError("Attach button not found")

        is_image = _is_image_file(path)
        menu_selectors = _ATTACH_MENU_MEDIA_SELECTORS if is_image else _ATTACH_MENU_FILE_SELECTORS

        async with page.expect_file_chooser(timeout=15000) as fc_info:
            await page.click(attach_selector)
            menu_selector = await self._wait_and_get_first(page, menu_selectors, timeout_ms=1500)
            if menu_selector:
                await page.click(menu_selector)
            else:
                fallback_selector = await self._wait_and_get_first(page, _ATTACH_MENU_FILE_SELECTORS, timeout_ms=800)
                if fallback_selector:
                    await page.click(fallback_selector)

        chooser = await fc_info.value
        await chooser.set_files(str(path))

        await self._wait_and_get_first(page, _ATTACHMENT_PREVIEW_SELECTORS, timeout_ms=15000)

    async def send_message(
        self,
        phone: str,
        text: str,
        attachment_path: Optional[Union[str, Path]] = None,
        base_url: str = DEFAULT_BASE_URL,
        chat_id: Optional[str] = None,
        use_chat_id: bool = False,
        humanize: bool = True,
    ) -> SendMaxMessageResult:
        try:
            context = await self.get_context()
            try:
                page = await context.new_page()
                try:
                    await self._attach_ws_sniffer_to_page(page, phone_for_mapping=phone)
                    await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)

                    if use_chat_id and chat_id:
                        if not await self._open_chat_by_chat_id(page, chat_id, base_url):
                            return SendMaxMessageResult(
                                sent_ok=False,
                                status_note="failed",
                                error_message=f"Existing chat not opened for chat_id={chat_id}",
                            )
                    else:
                        try:
                            if not await self._open_chat_by_phone(page, phone):
                                raise ContactNotFoundError(f"User not found by phone: {phone}")
                        except ContactNotFoundError as e:
                            return SendMaxMessageResult(
                                sent_ok=False,
                                status_note="user_not_found_by_phone",
                                error_message=str(e),
                            )

                    await self._maybe_capture_chat_id(page, phone)
                    if phone not in self._chat_by_phone:
                        await self._wait_for_chat_id(phone, timeout_seconds=5.0)

                    message_selector = await self._wait_and_get_first(page, _MESSAGE_INPUT_SELECTORS, timeout_ms=3000)
                    if not message_selector:
                        return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message="Input field not found")

                    if humanize:
                        human_delay = random.uniform(10, 30)
                        logger.info(f"Human-like delay {human_delay:.1f}s before typing")
                        await asyncio.sleep(human_delay)

                    await page.click(message_selector)
                    if attachment_path:
                        try:
                            await self._attach_file_to_chat(page, attachment_path)
                        except Exception as e:
                            return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message=str(e))
                    if humanize:
                        await self._type_like_human(page, message_selector, text)
                    else:
                        await page.fill(message_selector, text)
                    await page.keyboard.press("Enter")
                    try:
                        await page.wait_for_function(
                            """() => {
                                const wrappers = document.querySelectorAll('div[class*="messageWrapper"]');
                                if (!wrappers.length) return false;
                                const last = wrappers[wrappers.length - 1];
                                return (last.className || '').includes('messageWrapper--isOut');
                            }""",
                            timeout=5000,
                        )
                    except PlaywrightTimeoutError:
                        pass

                    return SendMaxMessageResult(
                        sent_ok=True,
                        status_note="delivered",
                        chat_id=self._chat_by_phone.get(phone) or chat_id,
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            finally:
                await context.close()
        except ContactNotFoundError as e:
            return SendMaxMessageResult(
                sent_ok=False,
                status_note="user_not_found_by_phone",
                error_message=str(e),
            )
        except Exception as e:
            logger.exception(f"Error sending message to {phone}")
            return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message=str(e))

    async def check_reply(self, phone: str, base_url: str = DEFAULT_BASE_URL) -> CheckMaxMessageResult:
        cached = self._get_cached_check_reply(phone)
        if cached:
            return cached
        try:
            context = await self.get_context()
            try:
                page = await context.new_page()
                try:
                    await self._attach_ws_sniffer_to_page(page, phone_for_mapping=None)
                    await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)

                    try:
                        opened = await self._open_chat_by_phone(page, phone)
                    except ContactNotFoundError:
                        return CheckMaxMessageResult(reply_value="", check_ok=False)
                    if not opened:
                        return CheckMaxMessageResult(reply_value="", check_ok=False)
                    await self._maybe_capture_chat_id(page, phone)

                    await self._wait_and_get_first(page, _CHAT_HISTORY_SELECTORS, timeout_ms=8000)
                    payload = await page.evaluate(_PARSE_LAST_MESSAGE_JS)
                    
                    if payload.get("error"):
                        return CheckMaxMessageResult(reply_value="", check_ok=False)

                    variant = (payload.get("variant") or "").strip().casefold()
                    is_out = bool(payload.get("isOut"))
                    text = str(payload.get("text") or "").strip()
                    status_icon = str(payload.get("statusIcon") or "")

                    logger.info(f"Debug: is_out={is_out}, variant={variant}, statusIcon={status_icon}")

                    is_viewed = False
                    status_icon_lower = status_icon.lower()
                    if is_out and (
                        "check-double" in status_icon_lower or
                        "read" in status_icon_lower or
                        "viewed" in status_icon_lower or
                        "seen" in status_icon_lower
                    ):
                        is_viewed = True
                    
                    if variant == "incoming" or not is_out:
                        return CheckMaxMessageResult(
                            reply_value=text, 
                            check_ok=True, 
                            replied_at=datetime.utcnow().isoformat() + "Z"
                        )
                    
                    return CheckMaxMessageResult(reply_value="", check_ok=True, is_viewed=is_viewed)
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            finally:
                await context.close()
        except Exception as e:
            logger.exception(f"Error checking reply for {phone}")
            return CheckMaxMessageResult(reply_value="", check_ok=False)

    async def _maybe_capture_chat_id(self, page: Page, phone: str) -> None:
        chat_id = _extract_chat_id_from_url(page.url)
        if not chat_id:
            try:
                chat_id = await page.evaluate(
                    """() => {
                      const fromUrl = () => {
                        const href = String(location.href || '');
                        const m1 = href.match(/(?:chatId=|chat_id=|chat\\/|chats\\/)(\\d+)/);
                        if (m1) return m1[1];
                        const m2 = href.match(/[#&?]c(?:hat)?=(\\d+)/);
                        if (m2) return m2[1];
                        return null;
                      };

                      const fromDom = () => {
                        const el =
                          document.querySelector('[data-chat-id]') ||
                          document.querySelector('[data-chatid]') ||
                          document.querySelector('.openedChat') ||
                          document.querySelector('[class*="openedChat"]');
                        if (!el) return null;
                        return (
                          el.getAttribute('data-chat-id') ||
                          el.getAttribute('data-chatid') ||
                          (el.dataset ? (el.dataset.chatId || el.dataset.chatid) : null) ||
                          null
                        );
                      };

                      return fromUrl() || fromDom();
                    }"""
                )
            except Exception:
                chat_id = None
        if not chat_id:
            return
        chat_id_str = str(chat_id)
        self._cache_phone_chat(phone=phone, chat_id=chat_id_str)

    def _cache_phone_chat(self, phone: str, chat_id: str) -> None:
        try:
            self._chat_by_phone[phone] = chat_id
            self._chat_by_phone.move_to_end(phone)
            self._phone_by_chat_id[chat_id] = phone
            self._phone_by_chat_id.move_to_end(chat_id)

            while len(self._chat_by_phone) > _PHONE_CHAT_CACHE_MAX_SIZE:
                old_phone, old_chat = self._chat_by_phone.popitem(last=False)
                if self._phone_by_chat_id.get(old_chat) == old_phone:
                    self._phone_by_chat_id.pop(old_chat, None)

            while len(self._phone_by_chat_id) > _PHONE_CHAT_CACHE_MAX_SIZE:
                old_chat, old_phone = self._phone_by_chat_id.popitem(last=False)
                if self._chat_by_phone.get(old_phone) == old_chat:
                    self._chat_by_phone.pop(old_phone, None)
        except Exception:
            return

    async def _wait_for_chat_id(self, phone: str, timeout_seconds: float) -> Optional[str]:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            chat_id = self._chat_by_phone.get(phone)
            if chat_id:
                return chat_id
            await asyncio.sleep(0.05)
        return None

    def _get_cached_check_reply(self, phone: str) -> Optional[CheckMaxMessageResult]:
        chat_id = self._chat_by_phone.get(phone)
        if not chat_id:
            return None
        st = self._chat_status.get(chat_id) or {}
        incoming_text = st.get("last_incoming_text")
        incoming_time = st.get("last_incoming_time")
        if isinstance(incoming_text, str) and incoming_text.strip():
            return CheckMaxMessageResult(
                reply_value=incoming_text,
                check_ok=True,
                replied_at=incoming_time or datetime.utcnow().isoformat() + "Z",
            )
        if st.get("is_viewed"):
            return CheckMaxMessageResult(reply_value="", check_ok=True, is_viewed=True)
        return CheckMaxMessageResult(reply_value="", check_ok=True)

    async def next_ws_action(self) -> Dict[str, Any]:
        return await self._ws_actions.get()

def normalize_phone_for_max(raw: Union[str, int, float, None]) -> Optional[str]:
    if raw is None: return None
    s = "".join(c for c in str(raw) if c.isdigit())
    if len(s) == 11:
        if s.startswith("8") or s.startswith("7"):
            s = s[1:]
    if len(s) == 10 and s.startswith("9"):
        return s
    return None

def _extract_chat_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:chatId=|chat/|chats/)(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[#&?]c(?:hat)?=(\d+)", url)
    if m:
        return m.group(1)
    return None

def _lz4_decompress(src: bytes, max_output_size: int) -> bytes:
    out = bytearray(max_output_size)
    si = 0
    oi = 0
    slen = len(src)
    while si < slen:
        token = src[si]
        si += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if si >= slen:
                    raise ValueError("lz4: corrupt block")
                b = src[si]
                si += 1
                lit_len += b
                if b != 255:
                    break
        if si + lit_len > slen or oi + lit_len > max_output_size:
            raise ValueError("lz4: corrupt block")
        out[oi : oi + lit_len] = src[si : si + lit_len]
        si += lit_len
        oi += lit_len
        if si == slen:
            break
        if si + 2 > slen:
            raise ValueError("lz4: corrupt block")
        offset = src[si] | (src[si + 1] << 8)
        si += 2
        if offset == 0 or offset > oi:
            raise ValueError("lz4: corrupt block")
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if si >= slen:
                    raise ValueError("lz4: corrupt block")
                b = src[si]
                si += 1
                match_len += b
                if b != 255:
                    break
        if oi + match_len > max_output_size:
            raise ValueError("lz4: corrupt block")
        ref = oi - offset
        if offset >= match_len:
            out[oi : oi + match_len] = out[ref : ref + match_len]
            oi += match_len
        else:
            end = oi + match_len
            while oi < end:
                out[oi] = out[ref]
                oi += 1
                ref += 1
    return bytes(out[:oi])

// decoding raw message
def _msgpack_decode(data: bytes) -> Any:
    def read(n: int) -> bytes:
        nonlocal pos
        if pos + n > len(data):
            raise ValueError("msgpack: truncated")
        b = data[pos : pos + n]
        pos += n
        return b

    def decode_one() -> Any:
        nonlocal pos
        b0 = data[pos]
        pos += 1

        if b0 <= 0x7F:
            return b0
        if b0 >= 0xE0:
            return b0 - 256

        if 0xA0 <= b0 <= 0xBF:
            ln = b0 & 0x1F
            return read(ln).decode("utf-8", errors="replace")

        if 0x90 <= b0 <= 0x9F:
            ln = b0 & 0x0F
            return [decode_one() for _ in range(ln)]

        if 0x80 <= b0 <= 0x8F:
            ln = b0 & 0x0F
            m: Dict[Any, Any] = {}
            for _ in range(ln):
                k = decode_one()
                v = decode_one()
                m[k] = v
            return m

        if b0 == 0xC0:
            return None
        if b0 == 0xC2:
            return False
        if b0 == 0xC3:
            return True

        if b0 == 0xCC:
            return struct.unpack(">B", read(1))[0]
        if b0 == 0xCD:
            return struct.unpack(">H", read(2))[0]
        if b0 == 0xCE:
            return struct.unpack(">I", read(4))[0]
        if b0 == 0xCF:
            return struct.unpack(">Q", read(8))[0]

        if b0 == 0xD0:
            return struct.unpack(">b", read(1))[0]
        if b0 == 0xD1:
            return struct.unpack(">h", read(2))[0]
        if b0 == 0xD2:
            return struct.unpack(">i", read(4))[0]
        if b0 == 0xD3:
            return struct.unpack(">q", read(8))[0]

        if b0 == 0xCA:
            return struct.unpack(">f", read(4))[0]
        if b0 == 0xCB:
            return struct.unpack(">d", read(8))[0]

        if b0 == 0xC4:
            ln = struct.unpack(">B", read(1))[0]
            return read(ln)
        if b0 == 0xC5:
            ln = struct.unpack(">H", read(2))[0]
            return read(ln)
        if b0 == 0xC6:
            ln = struct.unpack(">I", read(4))[0]
            return read(ln)

        if b0 == 0xD9:
            ln = struct.unpack(">B", read(1))[0]
            return read(ln).decode("utf-8", errors="replace")
        if b0 == 0xDA:
            ln = struct.unpack(">H", read(2))[0]
            return read(ln).decode("utf-8", errors="replace")
        if b0 == 0xDB:
            ln = struct.unpack(">I", read(4))[0]
            return read(ln).decode("utf-8", errors="replace")

        if b0 == 0xDC:
            ln = struct.unpack(">H", read(2))[0]
            return [decode_one() for _ in range(ln)]
        if b0 == 0xDD:
            ln = struct.unpack(">I", read(4))[0]
            return [decode_one() for _ in range(ln)]

        if b0 == 0xDE:
            ln = struct.unpack(">H", read(2))[0]
            m = {}
            for _ in range(ln):
                k = decode_one()
                v = decode_one()
                m[k] = v
            return m
        if b0 == 0xDF:
            ln = struct.unpack(">I", read(4))[0]
            m = {}
            for _ in range(ln):
                k = decode_one()
                v = decode_one()
                m[k] = v
            return m

        if b0 in (0xC7, 0xC8, 0xC9):
            if b0 == 0xC7:
                ln = struct.unpack(">B", read(1))[0]
            elif b0 == 0xC8:
                ln = struct.unpack(">H", read(2))[0]
            else:
                ln = struct.unpack(">I", read(4))[0]
            ext_type = struct.unpack(">b", read(1))[0]
            payload = read(ln)
            if ext_type == 1:
                try:
                    return int(_msgpack_decode(payload))
                except Exception:
                    return payload
            return payload

        raise ValueError(f"msgpack: unsupported type 0x{b0:02x}")

    pos = 0
    val = decode_one()
    return val

def _decode_max_binary_frame(raw: bytes) -> Optional[Dict[str, Any]]:
    if not raw or len(raw) < 10:
        return None
    proto = raw[0]
    cmd = raw[1]
    seq = struct.unpack(">h", raw[2:4])[0]
    opcode = struct.unpack(">h", raw[4:6])[0]
    factor = raw[6]
    payload_len = (raw[7] << 16) | (raw[8] << 8) | raw[9]
    if payload_len <= 0:
        return {"proto": proto, "cmd": cmd, "seq": seq, "opcode": opcode, "payload": None}
    payload_comp = raw[10 : 10 + payload_len]
    if len(payload_comp) != payload_len:
        return None
    try:
        payload_bytes = payload_comp
        if factor and factor > 0:
            payload_bytes = _lz4_decompress(payload_comp, payload_len * factor)
        payload = _msgpack_decode(payload_bytes)
    except Exception:
        payload = None
    return {"proto": proto, "cmd": cmd, "seq": seq, "opcode": opcode, "payload": payload}

def _extract_message_is_out(message: Dict[str, Any], viewer_id: Optional[int]) -> bool:
    if "isOut" in message:
        return bool(message.get("isOut"))
    if viewer_id is not None:
        for key in ("senderId", "sender", "authorId", "author", "fromUserId", "from"):
            if key not in message:
                continue
            sender = message.get(key)
            try:
                if int(sender) == int(viewer_id):
                    return True
            except Exception:
                continue
    return False

def _extract_message_text(message: Dict[str, Any]) -> str:
    v = message.get("text")
    if isinstance(v, str):
        return v.strip()
    content = message.get("content")
    if isinstance(content, dict):
        t = content.get("text")
        if isinstance(t, str):
            return t.strip()
    return ""

def _extract_message_time_iso(message: Dict[str, Any]) -> Optional[str]:
    t = message.get("time")
    if t is None:
        return None
    try:
        ts = float(t)
    except Exception:
        return None
    if ts > 1e12:
        ts = ts / 1000.0
    try:
        return datetime.utcfromtimestamp(ts).isoformat() + "Z"
    except Exception:
        return None
