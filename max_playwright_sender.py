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

_STATUS_HEADER_ALIASES_CASEFOLD = frozenset(
    {"статус max отправки", "статус max", "max отправлено", "отправлено max"}
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


def read_clients_from_xls(xls_path: Union[str, Path]) -> List[Tuple[int, str, str]]:
    """
    Читает клиентов из первого листа .xls: колонки «Имя» и «Телефон»
    (регистр не важен; если заголовков нет — первые две колонки).
    Возвращает тройки (индекс_строки_листа_как_в_xlrd, имя, телефон_для_max).
    Индекс строки — тот же, что нужен xlwt для записи в эту строку (0-based).
    """
    import xlrd  # type: ignore[import-untyped]

    path = Path(xls_path)
    if not path.exists():
        raise MaxMessengerError(f"Файл не найден: {path}")

    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_index(0)
    headers = [str(sheet.cell_value(0, c)).strip().casefold() for c in range(sheet.ncols)]
    idx_name = idx_phone = None
    for j, h in enumerate(headers):
        if h == "имя":
            idx_name = j
        elif h == "телефон":
            idx_phone = j
    if idx_name is None:
        idx_name = 0
    if idx_phone is None:
        idx_phone = 1

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


def write_row_status_to_xls(
    xls_path: Union[str, Path],
    row_index_xlrd: int,
    status_text: str,
) -> None:
    """
    Пишет отметку в первый лист того же .xls: колонка с заголовком «Статус MAX отправки»
    или уже существующая колонка с таким признаком иначе — новая самая правая колонка.
    """
    import xlrd  # type: ignore[import-untyped]
    from xlutils.copy import copy  # type: ignore[import-untyped]

    path = Path(xls_path)
    rb = xlrd.open_workbook(str(path), formatting_info=False)
    sheet0 = rb.sheet_by_index(0)
    headers_casefold = [
        str(sheet0.cell_value(0, c)).strip().casefold() for c in range(sheet0.ncols)
    ]
    status_col: Optional[int] = None
    for j, h in enumerate(headers_casefold):
        if h in _STATUS_HEADER_ALIASES_CASEFOLD:
            status_col = j
            break
    if status_col is None:
        status_col = sheet0.ncols

    wb = copy(rb)
    ws = wb.get_sheet(0)
    if status_col >= sheet0.ncols:
        ws.write(0, status_col, STATUS_COLUMN_HEADER)
    ws.write(row_index_xlrd, status_col, status_text)
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

    search_plus_button_selectors = [
        #"button[aria-label*='Начать общение']",
        #"button:has-text('Начать общение')",
        "button:has(use[href='#icon_plus_mini'])",
    ]
    find_by_number_item_menu_selectors = [
        "[role='menuitem']:has-text('Найти по номеру')",
        #"[role='button']:has-text('Найти по номеру')",
        #"button:has-text('Найти по номеру')",
        #"text=Найти по номеру",

        "[role='menuitem']:has-text('Search by number')",
        #"[role='button']:has-text('Search by number')",
        #"button:has-text('Search by number')",
        #"text=Search by number",
    ]

    """find_contact_form_selectors = [
        "form[id='findContact']",
        "form:has(button:has-text('Найти в MAX'))",
        "form:has-text('Найти в MAX')",
    ]"""

    contact_number_to_find_input_selectors = [
        "form[id='findContact'] input",
        #"form[id='findContact'] input[type='tel']",
        #"form[id='findContact'] input[name*='phone']",
        #"form input[placeholder*='номер']",
        #"form input[placeholder*='телефон']",
    ]
    find_contact_submit_selectors = [
        "button[aria-label*='Найти в MAX']",
        "button[aria-label*='Find in MAX']",
    ]

    message_input_selectors = [
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true'][data-testid*='composer']",
        "textarea[placeholder*='Сообщение']",
        "textarea[placeholder*='Message']",
        "div[placeholder*='Сообщение']",
        "div[placeholder*='Message']",
    ]

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

                # 1) Open "new chat/contact" menu by clicking "Начать общение".
                open_selector = await _wait_and_get_first(page, search_plus_button_selectors, timeout_ms=5000)
                if not open_selector:
                    raise MaxMessengerError("Button 'Начать общение' not found.")
                await page.click(open_selector)
                await page.wait_for_timeout(700)

                # 2) Click menu item "Найти по номеру" from dynamic popover.
                find_by_number_item_menu_selector = await _wait_and_get_first(
                    page, find_by_number_item_menu_selectors, timeout_ms=5000
                )
                if not find_by_number_item_menu_selector:
                    raise MaxMessengerError(
                        "Menu item 'Найти по номеру' not found after opening the menu."
                    )
                await page.click(find_by_number_item_menu_selector)
                await page.wait_for_timeout(600)

                contact_number_to_find_input_selector = await _wait_and_get_first(
                    page, contact_number_to_find_input_selectors, timeout_ms=8000
                )
                if not contact_number_to_find_input_selector:
                    raise MaxMessengerError("Phone input in form 'findContact' not found.")

                await page.click(contact_number_to_find_input_selector)
                await page.fill(contact_number_to_find_input_selector, "")
                await page.type(contact_number_to_find_input_selector, phone, delay=30)
                await page.wait_for_timeout(250)

                # 4) Submit the form by clicking "Найти в MAX".
                find_contact_submit_selector = await _wait_and_get_first(
                    page, find_contact_submit_selectors, timeout_ms=8000
                )
                if not find_contact_submit_selector:
                    raise MaxMessengerError("Submit button 'Найти в MAX' not found.")
                await page.click(find_contact_submit_selector)
                await page.wait_for_timeout(5800)

                message_selector = await _wait_and_get_first(
                    page, message_input_selectors, timeout_ms=1000
                )
                if not message_selector:
                    return SendMaxMessageResult(
                        sent_ok=False,
                        status_note="Не отправлено: поле ввода сообщения не найдено на странице",
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
        description="Отправка в MAX по номеру или рассылка из .xls (Klienty_301.xls и др.)."
    )
    parser.add_argument(
        "--xls",
        metavar="ФАЙЛ",
        default=None,
        help=(
            "Путь к Excel .xls: читаются строки клиентов, колонки «Имя» и «Телефон». "
            "Если передано только имя файла — ищется рядом с этим скриптом или по текущему каталогу."
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
