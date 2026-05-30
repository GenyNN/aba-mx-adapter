import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


DEFAULT_SESSION_FILE = "max_auth.json"
DEFAULT_BASE_URL = "https://web.max.ru"
#DEFAULT_PHONE = "9200204227"
DEFAULT_PHONE = "9159570933"
DEFAULT_TEXT = "Привет1_0"
DEFAULT_XLS_FILENAME = "Klienty_301.xls"
DEFAULT_MESSAGE_TEMPLATE = (
    "Здравствуйте, {name}! Напишите нам в ответ на это сообщение, если не хотите продолжить диалог."
)
STATUS_COLUMN_HEADER = "Статус MAX отправки"
STATUS_COLUMN_SENT_OK_LABEL = "Отправлено"
REPLY_COLUMN_HEADER = "Ответ пользователя"
REPLY_NOT_READ_LABEL = "не прочитано"
REPLY_READ_LABEL = "прочитано"

_STATUS_HEADER_ALIASES_CASEFOLD = frozenset(
    {"статус max отправки", "статус max", "max отправлено", "отправлено max"}
)
_REPLY_HEADER_ALIASES_CASEFOLD = frozenset(
    {"ответ пользователя", "ответ", "ответ max"}
)


class MaxMessengerError(RuntimeError):
    """Base error for Max messenger automation."""


class ContactNotFoundError(MaxMessengerError):
    """Raised when the phone number cannot be found."""


@dataclass(frozen=True)
class SendMaxMessageResult:
    """Результат одной попытки отправки (без падения если не найдено поле сообщения)."""

    sent_ok: bool
    status_note: str


@dataclass(frozen=True)
class CheckMaxMessageResult:
    """Результат проверки последнего сообщения в открытом чате."""

    reply_value: str
    check_ok: bool = True


CellValue = Union[str, float, int, None]


def _digits_from_excel_phone_cell(raw: CellValue) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        as_int = int(raw)
        if abs(raw - float(as_int)) > 1e-6:
            return "".join(c for c in str(raw) if c.isdigit())
        return str(as_int)
    if isinstance(raw, int):
        return str(raw)
    s = str(raw).strip()
    if not s:
        return None
    return "".join(c for c in s if c.isdigit())


def normalize_phone_for_max(raw: CellValue) -> Optional[str]:
    """
    Преобразует номер для поиска в MAX: строка из 10 цифр, начинается с «9»
    (мобильный РФ без ведущей «7» / «8»).
    """
    digits = _digits_from_excel_phone_cell(raw)
    if not digits:
        return None
    if len(digits) == 11:
        if digits.startswith("8"):
            digits = "7" + digits[1:]
        if digits.startswith("7"):
            digits = digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return digits
    return None


def _sheet_headers_casefold(sheet) -> List[str]:
    return [str(sheet.cell_value(0, c)).strip().casefold() for c in range(sheet.ncols)]


def _find_column_index(headers: List[str], names: frozenset, default: Optional[int] = None) -> Optional[int]:
    for j, h in enumerate(headers):
        if h in names:
            return j
    return default


def _load_sheet_indices(xls_path: Union[str, Path]) -> Tuple[object, object, int, int, Optional[int], Optional[int]]:
    """Возвращает (rb, sheet0, idx_name, idx_phone, idx_status, idx_reply)."""
    import xlrd  # type: ignore[import-untyped]

    path = Path(xls_path)
    rb = xlrd.open_workbook(str(path), formatting_info=False)
    sheet0 = rb.sheet_by_index(0)
    headers = _sheet_headers_casefold(sheet0)
    idx_name = _find_column_index(headers, frozenset({"имя"}), 0)
    idx_phone = _find_column_index(headers, frozenset({"телефон"}), 1)
    idx_status = _find_column_index(headers, _STATUS_HEADER_ALIASES_CASEFOLD)
    idx_reply = _find_column_index(headers, _REPLY_HEADER_ALIASES_CASEFOLD)
    return rb, sheet0, idx_name, idx_phone, idx_status, idx_reply


def _resolve_reply_column(sheet0, idx_status: Optional[int], idx_reply: Optional[int]) -> int:
    if idx_reply is not None:
        return idx_reply
    if idx_status is not None:
        return idx_status + 1
    return sheet0.ncols


def read_clients_from_xls(xls_path: Union[str, Path]) -> List[Tuple[int, str, str]]:
    """
    Читает клиентов из первого листа .xls: колонки «Имя» и «Телефон»
    (регистр не важен; если заголовков нет — первые две колонки).
    Возвращает тройки (индекс_строки_листа_как_в_xlrd, имя, телефон_для_max).
    Индекс строки — тот же, что нужен xlwt для записи в эту строку (0-based).
    """
    path = Path(xls_path)
    if not path.exists():
        raise MaxMessengerError(f"Файл не найден: {path}")

    _, sheet, idx_name, idx_phone, _, _ = _load_sheet_indices(path)

    out: List[Tuple[int, str, str]] = []
    for r in range(1, sheet.nrows):
        name_raw = sheet.cell_value(r, idx_name)
        phone_raw = sheet.cell_value(r, idx_phone)
        name = str(name_raw).strip() if name_raw is not None else ""
        phone = normalize_phone_for_max(phone_raw)
        if not name:
            continue
        if phone is None:
            continue
        out.append((r, name, phone))
    return out


def read_rows_for_mx_check(xls_path: Union[str, Path]) -> List[Tuple[int, str, str]]:
    """
    Строки для проверки: (индекс_строки, телефон_для_max, значение_колонки_«Статус MAX отправки»).
    """
    path = Path(xls_path)
    if not path.exists():
        raise MaxMessengerError(f"Файл не найден: {path}")

    _, sheet, _, idx_phone, idx_status, _ = _load_sheet_indices(path)
    out: List[Tuple[int, str, str]] = []
    for r in range(1, sheet.nrows):
        phone_raw = sheet.cell_value(r, idx_phone)
        phone = normalize_phone_for_max(phone_raw)
        if phone is None:
            continue
        status_text = ""
        if idx_status is not None:
            raw = sheet.cell_value(r, idx_status)
            status_text = str(raw).strip() if raw is not None else ""
        out.append((r, phone, status_text))
    return out


def _should_check_sent_status(send_status: str) -> bool:
    s = send_status.strip().casefold()
    if not s or "не отправлено" in s:
        return False
    return s == STATUS_COLUMN_SENT_OK_LABEL.casefold() or s.startswith(
        STATUS_COLUMN_SENT_OK_LABEL.casefold()
    )


def write_row_status_to_xls(
    xls_path: Union[str, Path],
    row_index_xlrd: int,
    status_text: str,
) -> None:
    """
    Пишет отметку в первый лист того же .xls: колонка с заголовком «Статус MAX отправки»
    или уже существующая колонка с таким признаком иначе — новая самая правая колонка.
    """
    path = Path(xls_path)
    rb, sheet0, _, _, idx_status, _ = _load_sheet_indices(path)
    status_col = idx_status if idx_status is not None else sheet0.ncols
    _write_xls_cell(path, rb, sheet0, row_index_xlrd, status_col, status_text, STATUS_COLUMN_HEADER)


def write_row_reply_to_xls(
    xls_path: Union[str, Path],
    row_index_xlrd: int,
    reply_text: str,
) -> None:
    """Пишет значение в колонку «Ответ пользователя» (сразу после колонки статуса отправки)."""
    path = Path(xls_path)
    rb, sheet0, _, _, idx_status, idx_reply = _load_sheet_indices(path)
    reply_col = _resolve_reply_column(sheet0, idx_status, idx_reply)
    _write_xls_cell(path, rb, sheet0, row_index_xlrd, reply_col, reply_text, REPLY_COLUMN_HEADER)


def _write_xls_cell(
    path: Path,
    rb: object,
    sheet0: object,
    row_index_xlrd: int,
    col_index: int,
    cell_text: str,
    header_if_new_col: str,
) -> None:
    from xlutils.copy import copy  # type: ignore[import-untyped]

    wb = copy(rb)
    ws = wb.get_sheet(0)
    if col_index >= sheet0.ncols:
        ws.write(0, col_index, header_if_new_col)
    else:
        existing_hdr = str(sheet0.cell_value(0, col_index)).strip().casefold()
        known = (
            _STATUS_HEADER_ALIASES_CASEFOLD
            if header_if_new_col == STATUS_COLUMN_HEADER
            else _REPLY_HEADER_ALIASES_CASEFOLD
        )
        if not existing_hdr or existing_hdr not in known:
            ws.write(0, col_index, header_if_new_col)
    ws.write(row_index_xlrd, col_index, cell_text)
    wb.save(str(path))


async def send_messages_from_xls(
    xls_path: Union[str, Path],
    *,
    message_template: str = DEFAULT_MESSAGE_TEMPLATE,
    headless: bool = True,
    delay_seconds: float = 3.0,
    session_file: str = DEFAULT_SESSION_FILE,
    base_url: str = DEFAULT_BASE_URL,
    attachment_path: Optional[str] = None,
) -> None:
    clients = read_clients_from_xls(xls_path)
    if not clients:
        raise MaxMessengerError("В файле нет ни одной строки с непустым именем и валидным телефоном.")

    path_obj = Path(xls_path)

    for i, (sheet_row_idx, name, phone) in enumerate(clients):
        text = message_template.format(name=name)
        result = await send_max_message(
            phone=phone,
            text=text,
            headless=headless,
            attachment_path=attachment_path,
            session_file=session_file,
            base_url=base_url,
        )
        if result.sent_ok:
            note = STATUS_COLUMN_SENT_OK_LABEL
        else:
            note = result.status_note
        write_row_status_to_xls(path_obj, sheet_row_idx, note)
        if i < len(clients) - 1 and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)


async def _block_heavy_resources(context: BrowserContext) -> None:
    """Block images, media and fonts to reduce network traffic."""
    blocked_resource_types = {"image", "media", "font"}

    async def route_handler(route):
        request = route.request
        if request.resource_type in blocked_resource_types:
            await route.abort()
            return

        url = request.url.lower()
        if any(ext in url for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".webm")):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_handler)


async def _wait_and_get_first(page: Page, selectors: Iterable[str], timeout_ms: int = 7000) -> Optional[str]:
    """Return first selector that appears on the page."""
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return selector
        except PlaywrightTimeoutError:
            continue
    return None


async def _upload_attachment(page: Page, file_path: str) -> None:
    """Загрузка файла через перехват системного диалога (без участия ОС)."""
    attachment = Path(file_path).expanduser()
    if not attachment.exists():
        raise MaxMessengerError(f"Файл не найден: {attachment}")

    # Селекторы кнопки "Скрепка"
    upload_button_selectors = [
        "button:has(use[href='#icon_clip_mini'])",
        "button:has(use[href='#icon_clip'])",
        "button[aria-label='Загрузить файл']",
        "button[aria-label='Upload file']",
    ]
    # Селекторы пункта "Файл" в появившемся меню
    upload_file_item_selectors = [
        "[role='menuitem']:has-text('Файл')",
        "[role='menuitem']:has-text('File')",
        "text=Файл",
    ]

    # 1. Находим и кликаем на "Скрепку"
    upload_selector = await _wait_and_get_first(page, upload_button_selectors, timeout_ms=6000)
    if not upload_selector:
        raise MaxMessengerError("Кнопка 'Скрепка' не найдена.")

    await page.click(upload_selector)
    # Небольшая пауза, чтобы Svelte успел отрендерить меню
    await page.wait_for_timeout(500)

    # 2. Находим пункт "Файл" и ПЕРЕХВАТЫВАЕМ выбор файла
    file_item_selector = await _wait_and_get_first(page, upload_file_item_selectors, timeout_ms=3000)
    if not file_item_selector:
        raise MaxMessengerError("Пункт меню 'Файл' не найден.")

    try:
        # expect_file_chooser магическим образом предотвращает появление окна macOS
        async with page.expect_file_chooser(timeout=5000) as chooser_info:
            await page.click(file_item_selector)

        chooser = await chooser_info.value
        await chooser.set_files(str(attachment))

        # 3. Ждем загрузки файла в превью и ОТПРАВЛЯЕМ
        # После выбора файла обычно появляется окно с кнопкой "Отправить"
        await page.wait_for_timeout(2000)  # Даем время на обработку файла браузером

        # Пробуем нажать Enter или найти кнопку "Отправить" в окне предпросмотра
        send_attachment_selectors = [
            "button:has(use[href='#icon_send'])",
            "button[aria-label='Отправить']",
            "div[role='button']:has-text('Отправить')",
        ]

        send_btn = await _wait_and_get_first(page, send_attachment_selectors, timeout_ms=3000)
        if send_btn:
            await page.click(send_btn)
        else:
            # Если спец-кнопку не нашли, просто жмем Enter
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(1000)  # Финальное ожидание ухода сообщения

    except PlaywrightTimeoutError:
        raise MaxMessengerError("Диалог выбора файла не открылся вовремя.")


# В функции send_max_message исправьте порядок, если нужно отправить текст ВМЕСТЕ с файлом:
# Но если вам нужно два сообщения (текст, потом файл), ваш текущий порядок в send_max_message подходит.

_SEARCH_PLUS_BUTTON_SELECTORS = [
    "button:has(use[href='#icon_plus_mini'])",
]
_FIND_BY_NUMBER_ITEM_MENU_SELECTORS = [
    "[role='menuitem']:has-text('Найти по номеру')",
    "[role='menuitem']:has-text('Search by number')",
]
_CONTACT_NUMBER_INPUT_SELECTORS = [
    "form[id='findContact'] input",
]
_FIND_CONTACT_SUBMIT_SELECTORS = [
    "button[aria-label*='Найти в MAX']",
    "button[aria-label*='Find in MAX']",
]
_MESSAGE_INPUT_SELECTORS = [
    "div[contenteditable='true'][role='textbox']",
    "div[contenteditable='true'][data-testid*='composer']",
    "textarea[placeholder*='Сообщение']",
    "textarea[placeholder*='Message']",
    "div[placeholder*='Сообщение']",
    "div[placeholder*='Message']",
]
_OPENED_CHAT_SELECTORS = [
    ".openedChat",
    "[class*='openedChat']",
]
_CHAT_HISTORY_SELECTORS = [
    ".openedChat .history",
    "[class*='openedChat'] [class*='history']",
]
_BACK_BUTTON_SELECTORS = [
    "button.backBtn",
    ".openedChat button.backBtn",
    "button:has(use[href='#icon_arrow_left'])",
]

_PARSE_LAST_MESSAGE_JS = """
() => {
  const chat = document.querySelector('.openedChat') ||
    document.querySelector('[class*="openedChat"]');
  if (!chat) return { error: 'no_chat' };
  const history = chat.querySelector('.history.svelte-3850xr') ||
    chat.querySelector('[class*="history"]');
  if (!history) return { error: 'no_history' };
  const wrappers = [...history.querySelectorAll('div[class*="messageWrapper"]')];
  if (!wrappers.length) return { error: 'no_messages' };
  const last = wrappers[wrappers.length - 1];
  const isOut = (last.className || '').includes('messageWrapper--isOut');
  const bubbleRoot = last.querySelector('[data-bubbles-variant]');
  const variant = bubbleRoot ? bubbleRoot.getAttribute('data-bubbles-variant') : null;
  const statusUse = last.querySelector('.indicators use');
  const statusIcon = statusUse ? (statusUse.getAttribute('href') || '') : '';
  const textEl = last.querySelector('.bubble span.text') ||
    last.querySelector('.bubble .text');
  let text = '';
  if (textEl) {
    text = (textEl.textContent || '').replace(/\\s+/g, ' ').trim();
  }
  return { isOut, variant, statusIcon, text };
}
"""


async def _leave_opened_chat_if_needed(page: Page) -> None:
    """Возврат к списку чатов перед поиском следующего номера (один браузер на много строк)."""
    if await page.locator(".openedChat, [class*='openedChat']").count() == 0:
        return
    back_selector = await _wait_and_get_first(page, _BACK_BUTTON_SELECTORS, timeout_ms=2000)
    if back_selector:
        await page.click(back_selector)
        await page.wait_for_timeout(600)


async def _open_chat_by_phone(page: Page, phone: str) -> bool:
    """Открывает чат по номеру (как при отправке). True, если появился openedChat или поле ввода."""
    await _leave_opened_chat_if_needed(page)
    open_selector = await _wait_and_get_first(page, _SEARCH_PLUS_BUTTON_SELECTORS, timeout_ms=5000)
    if not open_selector:
        raise MaxMessengerError("Button 'Начать общение' not found.")
    await page.click(open_selector)
    await page.wait_for_timeout(700)

    find_by_number_selector = await _wait_and_get_first(
        page, _FIND_BY_NUMBER_ITEM_MENU_SELECTORS, timeout_ms=5000
    )
    if not find_by_number_selector:
        raise MaxMessengerError("Menu item 'Найти по номеру' not found after opening the menu.")
    await page.click(find_by_number_selector)
    await page.wait_for_timeout(600)

    phone_input_selector = await _wait_and_get_first(
        page, _CONTACT_NUMBER_INPUT_SELECTORS, timeout_ms=8000
    )
    if not phone_input_selector:
        raise MaxMessengerError("Phone input in form 'findContact' not found.")

    await page.click(phone_input_selector)
    await page.fill(phone_input_selector, "")
    await page.type(phone_input_selector, phone, delay=30)
    await page.wait_for_timeout(250)

    submit_selector = await _wait_and_get_first(page, _FIND_CONTACT_SUBMIT_SELECTORS, timeout_ms=8000)
    if not submit_selector:
        raise MaxMessengerError("Submit button 'Найти в MAX' not found.")
    await page.click(submit_selector)
    await page.wait_for_timeout(5800)

    if await _wait_and_get_first(page, _OPENED_CHAT_SELECTORS, timeout_ms=8000):
        return True
    if await _wait_and_get_first(page, _MESSAGE_INPUT_SELECTORS, timeout_ms=3000):
        return True
    return False


def _reply_from_last_message_payload(payload: dict) -> CheckMaxMessageResult:
    if payload.get("error"):
        return CheckMaxMessageResult(
            reply_value=f"ошибка проверки: {payload['error']}",
            check_ok=False,
        )
    variant = (payload.get("variant") or "").strip().casefold()
    is_out = bool(payload.get("isOut"))
    status_icon = str(payload.get("statusIcon") or "")
    text = str(payload.get("text") or "").strip()

    if variant == "incoming" or not is_out:
        return CheckMaxMessageResult(reply_value=text or "(пустой ответ)")

    if "icon_status_read" in status_icon:
        return CheckMaxMessageResult(reply_value=REPLY_READ_LABEL)
    if "icon_status_delivered" in status_icon:
        return CheckMaxMessageResult(reply_value=REPLY_NOT_READ_LABEL)
    return CheckMaxMessageResult(reply_value=REPLY_NOT_READ_LABEL)


async def check_max_message_reply(
    phone: str,
    *,
    headless: bool = True,
    session_file: str = DEFAULT_SESSION_FILE,
    base_url: str = DEFAULT_BASE_URL,
) -> CheckMaxMessageResult:
    """Открывает чат по телефону и определяет статус последнего сообщения."""
    session_path = Path(session_file)
    if not session_path.exists():
        raise MaxMessengerError(f"Session file not found: {session_path}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(storage_state=str(session_path))
            try:
                await _block_heavy_resources(context)
                page = await context.new_page()
                await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2000)

                if not await _open_chat_by_phone(page, phone):
                    return CheckMaxMessageResult(
                        reply_value="ошибка проверки: чат не открыт",
                        check_ok=False,
                    )

                await _wait_and_get_first(page, _CHAT_HISTORY_SELECTORS, timeout_ms=8000)
                payload = await page.evaluate(_PARSE_LAST_MESSAGE_JS)
                return _reply_from_last_message_payload(payload)
            finally:
                await context.close()
                await browser.close()
    except PlaywrightTimeoutError as exc:
        raise MaxMessengerError(f"Timeout while checking Max web: {exc}") from exc
    except PlaywrightError as exc:
        raise MaxMessengerError(f"Playwright failed: {exc}") from exc


async def check_sent_messages_from_xls(
    xls_path: Union[str, Path],
    *,
    headless: bool = True,
    delay_seconds: float = 3.0,
    session_file: str = DEFAULT_SESSION_FILE,
    base_url: str = DEFAULT_BASE_URL,
) -> None:
    """
    Проверяет ответы по строкам, где «Статус MAX отправки» = «Отправлено».
    Строки с «Не отправлено» пропускаются. Результат пишется в «Ответ пользователя».
    """
    rows = read_rows_for_mx_check(xls_path)
    if not rows:
        raise MaxMessengerError("В файле нет строк с валидным телефоном.")

    path_obj = Path(xls_path)
    session_path = Path(session_file)
    if not session_path.exists():
        raise MaxMessengerError(f"Session file not found: {session_path}")

    to_check = [(r, phone, status) for r, phone, status in rows if _should_check_sent_status(status)]
    if not to_check:
        raise MaxMessengerError(
            'Нет строк для проверки: нужна колонка «Статус MAX отправки» со значением «Отправлено».'
        )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(storage_state=str(session_path))
            try:
                await _block_heavy_resources(context)
                page = await context.new_page()
                await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2000)

                for i, (sheet_row_idx, phone, _status) in enumerate(to_check):
                    try:
                        if not await _open_chat_by_phone(page, phone):
                            result = CheckMaxMessageResult(
                                reply_value="ошибка проверки: чат не открыт",
                                check_ok=False,
                            )
                        else:
                            await _wait_and_get_first(page, _CHAT_HISTORY_SELECTORS, timeout_ms=8000)
                            payload = await page.evaluate(_PARSE_LAST_MESSAGE_JS)
                            result = _reply_from_last_message_payload(payload)
                    except MaxMessengerError as exc:
                        result = CheckMaxMessageResult(
                            reply_value=f"ошибка проверки: {exc}",
                            check_ok=False,
                        )

                    write_row_reply_to_xls(path_obj, sheet_row_idx, result.reply_value)
                    if i < len(to_check) - 1 and delay_seconds > 0:
                        await asyncio.sleep(delay_seconds)
            finally:
                await context.close()
                await browser.close()
    except PlaywrightTimeoutError as exc:
        raise MaxMessengerError(f"Timeout while checking Max web: {exc}") from exc
    except PlaywrightError as exc:
        raise MaxMessengerError(f"Playwright failed: {exc}") from exc


async def send_max_message(
    phone: str,
    text: str,
    *,
    headless: bool = True,
    attachment_path: Optional[str] = None,
    session_file: str = DEFAULT_SESSION_FILE,
    base_url: str = DEFAULT_BASE_URL,
) -> SendMaxMessageResult:
    """
    Send a message to a Max contact by phone.

    Args:
        phone: Contact phone number in international format.
        text: Message text to send.
        attachment_path: Optional path to one file to attach.
        headless: Launch browser without UI when True.
        session_file: Path to Playwright storage state JSON.
        base_url: Max web URL.

    Returns:
        SendMaxMessageResult: если после поиска контакта не найден блок ввода сообщения —
        исключение не кидается, возвращается ``sent_ok=False`` с текстом для журнала/Excel.
        Остальные сбои (нет сессии, не открылся MAX и т.д.) по-прежнему через MaxMessengerError.
    """
    session_path = Path(session_file)
    if not session_path.exists():
        raise MaxMessengerError(f"Session file not found: {session_path}")

    send_button_selectors = [
        "button:has-text('Отправить')",
        "button:has-text('Send')",
        "button[aria-label*='Отправить']",
        "button[aria-label*='Send']",
        "[data-testid*='send']",
    ]

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(storage_state=str(session_path))
            try:
                await _block_heavy_resources(context)
                page = await context.new_page()

                await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2000)

                await _open_chat_by_phone(page, phone)

                message_selector = await _wait_and_get_first(
                    page, _MESSAGE_INPUT_SELECTORS, timeout_ms=1000
                )
                if not message_selector:
                    return SendMaxMessageResult(
                        sent_ok=False,
                        status_note="Не отправлено",
                    )

                await page.click(message_selector)
                if text:
                    await page.type(message_selector, text, delay=35)
                    await page.wait_for_timeout(300)

                # Prefer Enter. If UI blocks Enter behaviour, try explicit send button.
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(700)

                sent_hint = page.locator(f"text={text}").last
                if await sent_hint.count() == 0:
                    send_selector = await _wait_and_get_first(page, send_button_selectors, timeout_ms=2000)
                    if send_selector:
                        await page.click(send_selector)
                        await page.wait_for_timeout(700)

                if attachment_path:
                    await _upload_attachment(page, attachment_path)
                    await page.wait_for_timeout(5000)

                return SendMaxMessageResult(sent_ok=True, status_note=STATUS_COLUMN_SENT_OK_LABEL)
            finally:
                await context.close()
                await browser.close()
    except ContactNotFoundError:
        raise
    except PlaywrightTimeoutError as exc:
        raise MaxMessengerError(f"Timeout while interacting with Max web: {exc}") from exc
    except PlaywrightError as exc:
        raise MaxMessengerError(f"Playwright failed: {exc}") from exc


async def _demo() -> None:
    """Small runnable demo for local verification."""
    await send_max_message(
        phone=DEFAULT_PHONE,
        text=DEFAULT_TEXT,
        headless=False,  # set False to visually debug selectors
        attachment_path="/Users/evgenyushakov/Downloads/ps_MySQL_Certificate_1.pdf",
    )


def _resolve_xls_path(spec: str) -> Path:
    p = Path(spec).expanduser()
    if p.is_file():
        return p
    near_script = Path(__file__).resolve().parent / spec
    if near_script.is_file():
        return near_script
    return p


async def _cli_main_async() -> None:
    parser = argparse.ArgumentParser(
        description="Отправка в MAX по номеру, рассылка или проверка ответов из .xls."
    )
    parser.add_argument(
        "--mode",
        choices=["send", "check"],
        default="send",
        help="send: рассылка; check: проверка ответов по колонке статуса",
    )
    parser.add_argument(
        "--xls",
        metavar="ФАЙЛ",
        default=None,
        help=(
            "Путь к Excel .xls: колонки «Имя», «Телефон», «Статус MAX отправки». "
            "Если передано только имя файла — ищется рядом с этим скриптом."
        ),
    )
    parser.add_argument(
        "--template",
        "-t",
        default=DEFAULT_MESSAGE_TEMPLATE,
        help="Шаблон текста; подставляется только {name}",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Пауза между получателями, сек.",
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION_FILE,
        help="Файл сессии Playwright (storage state)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Без окна браузера (по умолчанию вкл.). Для отладки: --no-headless",
    )
    parser.add_argument("--attachment", default=None, help="Опциональный файл вложения для каждого")

    args = parser.parse_args()
    if args.xls:
        xls_path = _resolve_xls_path(args.xls)
        if args.mode == "check":
            await check_sent_messages_from_xls(
                xls_path,
                headless=args.headless,
                delay_seconds=args.delay,
                session_file=args.session,
            )
        else:
            await send_messages_from_xls(
                xls_path,
                message_template=args.template,
                headless=args.headless,
                delay_seconds=args.delay,
                session_file=args.session,
                attachment_path=args.attachment,
            )
    else:
        await _demo()


if __name__ == "__main__":
    asyncio.run(_cli_main_async())
