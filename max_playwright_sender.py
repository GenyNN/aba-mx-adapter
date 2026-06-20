import asyncio
import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Union

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

class MaxMessengerError(RuntimeError):
    """Base error for Max messenger automation."""

class ContactNotFoundError(MaxMessengerError):
    """Raised when the phone number cannot be found."""

@dataclass(frozen=True)
class SendMaxMessageResult:
    sent_ok: bool
    status_note: str
    error_message: str = ""

@dataclass(frozen=True)
class CheckMaxMessageResult:
    reply_value: str
    check_ok: bool = True
    replied_at: Optional[str] = None

# --- Selectors ---
_SEARCH_PLUS_BUTTON_SELECTORS = ["button:has(use[href='#icon_plus_mini'])"]
_FIND_BY_NUMBER_ITEM_MENU_SELECTORS = ["[role='menuitem']:has-text('Найти по номеру')", "[role='menuitem']:has-text('Search by number')"]
_CONTACT_NUMBER_INPUT_SELECTORS = ["form[id='findContact'] input"]
_FIND_CONTACT_SUBMIT_SELECTORS = ["button[aria-label*='Найти в MAX']", "button[aria-label*='Find in MAX']"]
_MESSAGE_INPUT_SELECTORS = [
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][data-testid*='composer']",
    "textarea[placeholder*='Сообщение']",
    "textarea[placeholder*='Message']",
    "div[placeholder*='Message']"
]
#"div[placeholder*='Messagne'
_OPENED_CHAT_SELECTORS = [".openedChat", "[class*='openedChat']"]
_CHAT_HISTORY_SELECTORS = [".openedChat .history", "[class*='openedChat'] [class*='history']"]
_BACK_BUTTON_SELECTORS = ["button.backBtn", "button:has(use[href='#icon_arrow_left'])"]

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
    def __init__(self, session_file: str = DEFAULT_SESSION_FILE, headless: bool = True, max_tasks: int = 50):
        self.session_file = session_file
        self.headless = headless
        self.max_tasks = max_tasks
        self.tasks_count = 0
        self.playwright = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self):
        if not self.browser:
            logger.info("Starting Chromium browser...")
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                channel="chrome"
            )
            self.tasks_count = 0

    async def stop(self):
        if self.browser:
            logger.info("Closing Chromium browser...")
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def get_context(self) -> BrowserContext:
        async with self._lock:
            self.tasks_count += 1
            if self.tasks_count > self.max_tasks:
                logger.info(f"Task limit ({self.max_tasks}) reached. Restarting browser...")
                await self.stop()
                await self.start()
            
            if not self.browser:
                await self.start()
                
            session_path = Path(self.session_file)
            if not session_path.exists():
                raise MaxMessengerError(f"Session file not found: {session_path}")
                
            context = await self.browser.new_context(storage_state=str(session_path))
            await self._block_heavy_resources(context)
            return context

    async def _block_heavy_resources(self, context: BrowserContext) -> None:
        blocked_resource_types = {"image", "media", "font"}
        blocked_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm"}

        async def route_handler(route):
            request = route.request
            if request.resource_type in blocked_resource_types:
                await route.abort()
                return
            url = request.url.lower()
            if any(ext in url for ext in blocked_extensions):
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)

    async def _wait_and_get_first(self, page: Page, selectors: Iterable[str], timeout_ms: int = 7000) -> Optional[str]:
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=timeout_ms)
                return selector
            except PlaywrightTimeoutError:
                continue
        return None

    async def _open_chat_by_phone(self, page: Page, phone: str) -> bool:
        logger.info(f"Opening chat for phone: {phone}")
        # Return to chat list if needed
        if await page.locator(".openedChat, [class*='openedChat']").count() > 0:
            back_selector = await self._wait_and_get_first(page, _BACK_BUTTON_SELECTORS, timeout_ms=2000)
            if back_selector:
                await page.click(back_selector)
                await page.wait_for_timeout(600)

        open_selector = await self._wait_and_get_first(page, _SEARCH_PLUS_BUTTON_SELECTORS, timeout_ms=5000)
        if not open_selector:
            raise MaxMessengerError("Button 'Начать общение' not found.")
        await page.click(open_selector)
        await page.wait_for_timeout(700)

        find_by_number_selector = await self._wait_and_get_first(page, _FIND_BY_NUMBER_ITEM_MENU_SELECTORS, timeout_ms=5000)
        if not find_by_number_selector:
            raise MaxMessengerError("Menu item 'Найти по номеру' not found.")
        await page.click(find_by_number_selector)
        await page.wait_for_timeout(600)

        phone_input_selector = await self._wait_and_get_first(page, _CONTACT_NUMBER_INPUT_SELECTORS, timeout_ms=8000)
        if not phone_input_selector:
            raise MaxMessengerError("Phone input not found.")

        await page.click(phone_input_selector)
        await page.fill(phone_input_selector, "")
        await page.type(phone_input_selector, phone, delay=30)
        await page.wait_for_timeout(250)

        submit_selector = await self._wait_and_get_first(page, _FIND_CONTACT_SUBMIT_SELECTORS, timeout_ms=8000)
        if not submit_selector:
            raise MaxMessengerError("Submit button 'Найти в MAX' not found.")
        await page.click(submit_selector)
        
        # Long wait for chat to open
        chat_found = await self._wait_and_get_first(page, _OPENED_CHAT_SELECTORS + _MESSAGE_INPUT_SELECTORS, timeout_ms=10000)
        return bool(chat_found)

    async def send_message(self, phone: str, text: str, base_url: str = DEFAULT_BASE_URL) -> SendMaxMessageResult:
        context = await self.get_context()
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1000)

            if not await self._open_chat_by_phone(page, phone):
                return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message="Chat not opened")

            message_selector = await self._wait_and_get_first(page, _MESSAGE_INPUT_SELECTORS, timeout_ms=3000)
            if not message_selector:
                return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message="Input field not found")

            await page.click(message_selector)
            await page.type(message_selector, text, delay=35)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)

            return SendMaxMessageResult(sent_ok=True, status_note="delivered")
        except Exception as e:
            logger.exception(f"Error sending message to {phone}")
            return SendMaxMessageResult(sent_ok=False, status_note="failed", error_message=str(e))
        finally:
            await context.close()

    async def check_reply(self, phone: str, base_url: str = DEFAULT_BASE_URL) -> CheckMaxMessageResult:
        context = await self.get_context()
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1000)

            if not await self._open_chat_by_phone(page, phone):
                return CheckMaxMessageResult(reply_value="", check_ok=False)

            await self._wait_and_get_first(page, _CHAT_HISTORY_SELECTORS, timeout_ms=8000)
            payload = await page.evaluate(_PARSE_LAST_MESSAGE_JS)
            
            if payload.get("error"):
                return CheckMaxMessageResult(reply_value="", check_ok=False)

            variant = (payload.get("variant") or "").strip().casefold()
            is_out = bool(payload.get("isOut"))
            text = str(payload.get("text") or "").strip()
            
            # If last message is incoming (from client)
            if variant == "incoming" or not is_out:
                return CheckMaxMessageResult(
                    reply_value=text, 
                    check_ok=True, 
                    replied_at=datetime.utcnow().isoformat() + "Z"
                )
            
            return CheckMaxMessageResult(reply_value="", check_ok=True)
        except Exception as e:
            logger.exception(f"Error checking reply for {phone}")
            return CheckMaxMessageResult(reply_value="", check_ok=False)
        finally:
            await context.close()

def normalize_phone_for_max(raw: Union[str, int, float, None]) -> Optional[str]:
    if raw is None: return None
    s = "".join(c for c in str(raw) if c.isdigit())
    if len(s) == 11:
        if s.startswith("8") or s.startswith("7"):
            s = s[1:]
    if len(s) == 10 and s.startswith("9"):
        return s
    return None
