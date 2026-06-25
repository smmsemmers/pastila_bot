"""
Pastila OS — Task Bot
Заводит задачи через диалог с кнопками, постит в нужный топик группы и пишет в Google Sheets.
"""

import os
import re
import html
import asyncio
import logging
import datetime
import json
from zoneinfo import ZoneInfo

import base64
import io
import hashlib

import httpx
import llm_router as llm
import gspread

try:
    import pdfplumber
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

try:
    from docx import Document as DocxDocument
    _DOCX_OK = True
except ImportError:
    _DOCX_OK = False

try:
    from notion_client import AsyncClient as NotionClient
    _NOTION_SDK_OK = True
except ImportError:
    _NOTION_SDK_OK = False
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    BotCommand,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------
# ЛОГИ
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# КОНФИГ — берётся из переменных окружения (Render → Environment)
# ------------------------------------------------------------------
def _int_env(name, default):
    """int из env с безопасным фолбэком на default."""
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        logger.warning("Неверное значение %s — использую %s", name, default)
        return default


BOT_TOKEN = os.environ["BOT_TOKEN"]                  # обязателен — без него не запуститься
# Остальные id можно задать позже: бот стартует и с одним BOT_TOKEN, чтобы через /id
# собрать chat_id и id топиков. Чего не хватает — будет видно в логах при старте.
GROUP_CHAT_ID = _int_env("GROUP_CHAT_ID", 0)         # id группы (-100…)
THREAD_LENA = _int_env("THREAD_LENA", 0)             # message_thread_id топика Лены
THREAD_GLEB = _int_env("THREAD_GLEB", 0)             # message_thread_id топика Глеба
SHEET_ID = os.environ.get("SHEET_ID", "")            # id Google-таблицы
SHEET_NAME = os.environ.get("SHEET_NAME", "Sheet1")  # имя листа

# Юзернеймы для тегов в задаче
TAG_LENA = os.environ.get("TAG_LENA", "@elenaisanewleet")
TAG_GLEB = os.environ.get("TAG_GLEB", "@foxruso")


# Утренний дайджест (#3): во сколько и в какой таймзоне слать
DIGEST_HOUR = _int_env("DIGEST_HOUR", 12)
DIGEST_MINUTE = _int_env("DIGEST_MINUTE", 0)
TIMEZONE = os.environ.get("TZ", "Europe/Moscow")
try:
    TZINFO = ZoneInfo(TIMEZONE)
except Exception:
    # если в системе нет базы таймзон — Москва это фиксированный UTC+3 (без перехода на лето)
    logger.warning("Таймзона %s недоступна — использую фиксированный UTC+3 (МСК)", TIMEZONE)
    TZINFO = datetime.timezone(datetime.timedelta(hours=3))

# Дедлайн-алерты (#4): во сколько проверять задачи с дедлайном на завтра
ALERT_HOUR = _int_env("ALERT_HOUR", 12)
ALERT_MINUTE = _int_env("ALERT_MINUTE", 0)

# Голосовой ввод (#6): ключ OpenAI (Whisper + GPT). Пусто → фича выключена.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

# Notion: NOTION_TOKEN — ключ интеграции (Internal Integration Secret)
# NOTION_PAGES — page_id через запятую; если пусто — sync берёт все accessible pages
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_PAGES = [p.strip() for p in os.environ.get("NOTION_PAGES", "").split(",") if p.strip()]
VISION_MODEL_KEY = os.environ.get("VISION_MODEL", "haiku45")  # модель для описания картинок

# Детектор важных мыслей
# INSIGHT_NOTIFY=true  → бот пишет короткий комментарий в чат
# INSIGHT_NOTIFY=false → только ⭐-реакция + тихое сохранение в KB (менее шумно)
INSIGHT_NOTIFY = os.environ.get("INSIGHT_NOTIFY", "true").lower() in ("1", "true", "yes")

# ------------------------------------------------------------------
# GOOGLE SHEETS — подключение
# ------------------------------------------------------------------
def get_worksheet():
    """Открывает лист Google-таблицы через сервисный ключ."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


# ── Персистентность состояния LLM-роутера (отдельная вкладка таблицы) ──
_LLM_CONFIG_TAB = "llm_config"


def _llm_config_ws():
    """Вкладка для конфига роутера (создаётся при отсутствии)."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scopes=scopes)
    ss = gspread.authorize(creds).open_by_key(SHEET_ID)
    try:
        return ss.worksheet(_LLM_CONFIG_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=_LLM_CONFIG_TAB, rows=2, cols=1)
        ws.update_acell("A1", "{}")
        return ws


def _load_llm_state():
    try:
        raw = _llm_config_ws().acell("A1").value or "{}"
        llm.import_state(json.loads(raw))
        logger.info("Состояние LLM-роутера загружено.")
    except Exception as e:
        logger.warning("Не удалось загрузить состояние LLM: %s", e)


def _save_llm_state():
    try:
        _llm_config_ws().update_acell(
            "A1", json.dumps(llm.export_state(), ensure_ascii=False))
    except Exception as e:
        logger.warning("Не удалось сохранить состояние LLM: %s", e)


_KB_TAB = "_knowledge"
_KB_HEADERS = ["source", "item_id", "title", "content", "added_at", "tags"]


def _knowledge_ws():
    """Возвращает вкладку _knowledge, создаёт если нет."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json or not SHEET_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(_KB_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(_KB_TAB, rows=1000, cols=len(_KB_HEADERS))
            ws.append_row(_KB_HEADERS, value_input_option="USER_ENTERED")
        return ws
    except Exception as e:
        logger.error("_knowledge_ws: %s", e)
        return None


def _load_knowledge():
    """Загружает базу знаний из Sheet в память при старте."""
    global _KNOWLEDGE, _KNOWLEDGE_IDS
    ws = _knowledge_ws()
    if not ws:
        return
    try:
        rows = ws.get_all_records()
        _KNOWLEDGE = [{k: str(v) for k, v in r.items()} for r in rows if r.get("item_id")]
        _KNOWLEDGE_IDS = {r["item_id"] for r in _KNOWLEDGE}
        logger.info("База знаний загружена: %d элементов", len(_KNOWLEDGE))
    except Exception as e:
        logger.error("_load_knowledge: %s", e)


def _add_knowledge_item(source: str, item_id: str, title: str, content: str,
                        tags=None):
    """Добавляет элемент в память и в Sheet (если ещё не было)."""
    if item_id in _KNOWLEDGE_IDS:
        return False
    now = datetime.datetime.now(TZINFO).strftime("%Y-%m-%d %H:%M")
    tags_str = " ".join(f"#{t.lstrip('#')}" for t in (tags or []))
    item = {"source": source, "item_id": item_id, "title": title,
            "content": content, "added_at": now, "tags": tags_str}
    _KNOWLEDGE.append(item)
    _KNOWLEDGE_IDS.add(item_id)
    try:
        ws = _knowledge_ws()
        if ws:
            ws.append_row([source, item_id, title, content, now, tags_str],
                          value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error("_add_knowledge_item (sheet): %s", e)
    return True


def _kb_context(max_chars_per_item: int = 800, max_total: int = 8000,
                filter_tags=None) -> str:
    """Форматирует базу знаний для вставки в промпт LLM. Опционально фильтрует по тегам."""
    if not _KNOWLEDGE:
        return ""
    items = _KNOWLEDGE
    if filter_tags:
        ft = [t.lower().lstrip("#") for t in filter_tags]
        items = [i for i in items if any(
            t in (i.get("tags", "") + " " + i.get("title", "") + " " + i.get("content", "")).lower()
            for t in ft
        )]
    if not items:
        return ""
    parts = []
    total = 0
    for item in items:
        snippet = item["content"][:max_chars_per_item].strip()
        if len(item["content"]) > max_chars_per_item:
            snippet += "…"
        tags_part = f"  [{item['tags']}]" if item.get("tags") else ""
        label = f"[{item['source'].upper()}: {item['title']}{tags_part}]"
        block = f"{label}\n{snippet}"
        if total + len(block) > max_total:
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return ""
    return "\n\n=== БАЗА ЗНАНИЙ КОМАНДЫ ===\n" + "\n\n".join(parts) + "\n=== КОНЕЦ БЗ ==="


def _kb_search(query: str, max_results: int = 5) -> list[dict]:
    """Простой полнотекстовый поиск по базе знаний."""
    q = query.lower()
    words = [w.lstrip("#") for w in q.split() if len(w) > 2]
    scored = []
    for item in _KNOWLEDGE:
        haystack = (
            item.get("title", "") + " " +
            item.get("tags", "") + " " +
            item.get("content", "")
        ).lower()
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return [item for _, item in scored[:max_results]]


def purge_all_tasks() -> int:
    """Удаляет все строки с задачами из таблицы, оставляя заголовок. Возвращает кол-во удалённых."""
    try:
        ws = get_worksheet()
        all_rows = ws.get_all_values()
        if len(all_rows) <= 1:
            return 0
        count = len(all_rows) - 1
        # Оставляем только первую строку (заголовок)
        ws.resize(rows=1)
        logger.info("Purge: удалено %d задач из таблицы", count)
        return count
    except Exception as e:
        logger.error("purge_all_tasks: %s", e)
        return -1


def append_task_to_sheet(date_str, who, task_title, deadline, status, link=""):
    """Добавляет строку в таблицу: Дата | Кто | Задача | Дедлайн | Статус | Ссылка."""
    try:
        ws = get_worksheet()
        ws.append_row(
            [date_str, who, task_title, deadline, status, link],
            value_input_option="USER_ENTERED",
        )
        logger.info("Строка добавлена в таблицу: %s", task_title)
        return True
    except Exception as e:
        logger.error("Ошибка записи в таблицу: %s", e)
        return False


# Статусы, которые считаем «закрытыми» — такие задачи в /list не показываем
CLOSED_MARKERS = ("DONE", "CANCELLED")


def read_open_tasks():
    """Читает таблицу и группирует открытые задачи по исполнителю.

    Открытая = статус не содержит DONE/CANCELLED.
    Возвращает dict {кто: [ {title, deadline, status}, ... ]}.
    Функция блокирующая (gspread синхронный) — вызывать через asyncio.to_thread.
    """
    ws = get_worksheet()
    records = ws.get_all_records()  # первая строка таблицы — заголовки
    groups = {}
    for row in records:
        status = str(row.get("Статус", "")).strip()
        if any(marker in status.upper() for marker in CLOSED_MARKERS):
            continue
        who = str(row.get("Кто", "")).strip() or "—"
        groups.setdefault(who, []).append(
            {
                "title": str(row.get("Задача", "")).strip(),
                "deadline": str(row.get("Дедлайн", "")).strip() or "Backlog",
                "status": status,
            }
        )
    return groups


def update_task_status(title, new_status):
    """Находит строку задачи по названию и обновляет её статус.

    Если задач с таким названием несколько — берём последнюю (самую свежую).
    Возвращает True, если строка найдена и обновлена, иначе False.
    Блокирующая функция — вызывать через asyncio.to_thread.
    """
    ws = get_worksheet()
    values = ws.get_all_values()  # первая строка — заголовки
    target_row = None
    for idx, row in enumerate(values):
        if idx == 0:
            continue  # пропускаем заголовок
        # колонки: Дата(1) Кто(2) Задача(3) Дедлайн(4) Статус(5) Ссылка(6)
        if len(row) >= 3 and row[2].strip() == title.strip():
            target_row = idx + 1  # строки в gspread 1-индексные
    if target_row is None:
        return False
    ws.update_cell(target_row, 5, new_status)  # 5 — колонка «Статус»
    return True


def read_tasks_due(date_str):
    """Открытые задачи с дедлайном = date_str (формат «ДД.ММ»).

    Возвращает список dict {who, title, status}. Блокирующая — через asyncio.to_thread.
    """
    ws = get_worksheet()
    records = ws.get_all_records()  # первая строка — заголовки
    due = []
    for row in records:
        status = str(row.get("Статус", "")).strip()
        if any(m in status.upper() for m in CLOSED_MARKERS):
            continue
        # сравниваем нормализованный дедлайн строки с искомой датой
        if parse_deadline(str(row.get("Дедлайн", ""))) == date_str:
            due.append(
                {
                    "who": str(row.get("Кто", "")).strip() or "—",
                    "title": str(row.get("Задача", "")).strip(),
                    "status": status,
                }
            )
    return due


def read_due_today(today=None):
    """Открытые задачи с дедлайном = сегодня (по умолчанию — текущая дата в TZINFO)."""
    if today is None:
        today = datetime.datetime.now(TZINFO).strftime("%d.%m")
    return read_tasks_due(today)


# ------------------------------------------------------------------
# СОСТОЯНИЯ ДИАЛОГА
# ------------------------------------------------------------------
(
    TITLE,
    DOD,
    WHO,
    DEADLINE,
    STEPS,
    MATERIALS,
    TAGS,
    STATUS,
    SESSION_TITLE,
    SESSION_CONTENT,
) = range(10)

# Варианты для кнопок
WHO_OPTIONS = ["Лена", "Глеб", "Лена + Глеб"]
STATUS_OPTIONS = [
    "⚪️ NEW",
    "🟡 TODO",
    "🔵 WIP",
    "🟠 WAITING",
    "🟣 REVIEW",
    "🟢 DONE",
    "🔴 BLOCKED",
    "⚫️ CANCELLED",
]

# Быстрые статусы для кнопок прямо под задачей (#8): код → подпись на кнопке
QUICK_STATUS = [
    ("WIP", "🔵 В работу"),
    ("WAITING", "🟠 Ждём"),
    ("REVIEW", "🟣 Ревью"),
    ("BLOCKED", "🔴 Блок"),
    ("DONE", "✅ Done"),
]
# код → каноничный статус из STATUS_OPTIONS (например "DONE" → "🟢 DONE")
STATUS_BY_CODE = {opt.split(" ", 1)[1]: opt for opt in STATUS_OPTIONS}


# ------------------------------------------------------------------
# ХЕЛПЕРЫ ДЛЯ КЛАВИАТУР
# ------------------------------------------------------------------
def who_keyboard():
    buttons = [[InlineKeyboardButton(o, callback_data=f"who::{o}")] for o in WHO_OPTIONS]
    return InlineKeyboardMarkup(buttons)


def status_keyboard(prefix="status"):
    # по две кнопки в ряд, чтобы компактнее.
    # prefix задаёт callback_data: "status" — для нового таска, "setstatus" — для смены статуса.
    rows = []
    for i in range(0, len(STATUS_OPTIONS), 2):
        row = [
            InlineKeyboardButton(STATUS_OPTIONS[i], callback_data=f"{prefix}::{STATUS_OPTIONS[i]}")
        ]
        if i + 1 < len(STATUS_OPTIONS):
            row.append(
                InlineKeyboardButton(
                    STATUS_OPTIONS[i + 1], callback_data=f"{prefix}::{STATUS_OPTIONS[i + 1]}"
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def skip_keyboard(step):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏭ Пропустить", callback_data=f"skip::{step}")]]
    )


def quick_status_keyboard():
    """Кнопки быстрой смены статуса под опубликованной задачей."""
    buttons = [
        InlineKeyboardButton(label, callback_data=f"quick::{code}")
        for code, label in QUICK_STATUS
    ]
    # по 3 в ряд, чтобы не было тесно
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------
# СБОРКА ТЕКСТА ЗАДАЧИ
# ------------------------------------------------------------------
def build_task_text(data):
    lines = []
    lines.append(f"📌  {data.get('title', '').upper()}")
    if data.get("dod"):
        lines.append(f"✔  Готово когда: {data['dod']}")
    lines.append("─" * 28)
    lines.append(f"👤  {data.get('who', '')}   ·   🗓 {data.get('deadline', 'Backlog')}")
    if data.get("steps"):
        lines.append("")
        lines.append("Шаги:")
        for step in data["steps"].split("\n"):
            step = step.strip()
            if step:
                lines.append(f"  · {step}")
    materials = data.get("materials") or ""
    if materials and materials != "—":
        lines.append(f"\n📎  {materials}")
    if data.get("tags"):
        lines.append(f"🏷  {data['tags']}")
    lines.append("─" * 28)
    lines.append(f"{data.get('status', '🟡 TODO')}")
    return "\n".join(lines)


def extract_task_title(text):
    """Достаёт название из текста опубликованной задачи (строка «📌 ЗАДАЧА: …»)."""
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("📌 ЗАДАЧА:"):
            return line.split(":", 1)[1].strip()
    return ""


def replace_status_line(text, new_status):
    """Меняет строку статуса в тексте задачи на new_status.
    Возвращает новый текст или None, если строки статуса там нет."""
    lines = text.split("\n")
    statuses = set(STATUS_OPTIONS)
    for i in range(len(lines) - 1, -1, -1):  # статус — в конце, ищем с конца
        if lines[i].strip() in statuses:
            lines[i] = new_status
            return "\n".join(lines)
    return None


def parse_deadline(text):
    """Проверяет дедлайн в формате ДД.ММ (год необязателен и отбрасывается).
    Допускает разделители . - / и одно-/двузначные числа.
    Возвращает нормализованную строку «ДД.ММ» или None, если формат неверный."""
    text = "".join(text.split())  # убираем пробелы
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/]\d{2,4})?", text)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    # максимум дней в месяце (февраль допускаем до 29, год неизвестен)
    days_in_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not 1 <= day <= days_in_month[month - 1]:
        return None
    return f"{day:02d}.{month:02d}"


def message_link(chat_id, message_id, thread_id=None):
    """Строит deep-link на сообщение супергруппы:
    t.me/c/<internal>/[<thread>/]<message_id>, где internal — id чата без префикса -100."""
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


# ------------------------------------------------------------------
# ДИАЛОГ
# ------------------------------------------------------------------
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🆕 Новая задача.\n\nНапиши короткое название задачи:"
    )
    return TITLE


async def cmd_new_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск /new по кнопке «➕ Новая задача» из меню — точка входа в тот же диалог."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text(
        "🆕 Новая задача.\n\nНапиши короткое название задачи:"
    )
    return TITLE


async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "🏁 Критерий готовности (DoD) — когда задачу можно считать сделанной?\n\n"
        "Напиши текстом или пропусти:",
        reply_markup=skip_keyboard("dod"),
    )
    return DOD


async def get_dod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["dod"] = update.message.text.strip()
    await update.message.reply_text("👤 На кого задача?", reply_markup=who_keyboard())
    return WHO


async def get_who_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    who = query.data.split("::", 1)[1]
    context.user_data["who"] = who
    await query.edit_message_text(f"👤 На кого: {who}")
    await query.message.reply_text(
        "🗓️ Дедлайн в формате ДД.ММ (например 25.07).\n\n"
        "Напиши текстом или пропусти (уйдёт в Backlog):",
        reply_markup=skip_keyboard("deadline"),
    )
    return DEADLINE


async def get_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deadline = parse_deadline(update.message.text)
    if deadline is None:
        # формат не распознан — просим ввести заново (или пропустить)
        await update.message.reply_text(
            "🗓️ Не понял дату. Нужен формат ДД.ММ, например 25.07.\n\n"
            "Попробуй ещё раз или пропусти (уйдёт в Backlog):",
            reply_markup=skip_keyboard("deadline"),
        )
        return DEADLINE
    context.user_data["deadline"] = deadline
    await update.message.reply_text(
        "📋 Что сделать? Можешь перечислить шаги — каждый с новой строки.\n\n"
        "Напиши текстом или пропусти:",
        reply_markup=skip_keyboard("steps"),
    )
    return STEPS


async def get_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["steps"] = update.message.text.strip()
    await update.message.reply_text(
        "📎 Материалы — файл или ссылка?\n\nНапиши текстом или пропусти:",
        reply_markup=skip_keyboard("materials"),
    )
    return MATERIALS


async def get_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["materials"] = update.message.text.strip()
    await update.message.reply_text(
        "🏷️ Теги (например: #excel #клиент #баг).\n\nНапиши текстом или пропусти:",
        reply_markup=skip_keyboard("tags"),
    )
    return TAGS


async def get_materials_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """На шаге материалов прислали файл/фото — запоминаем его, чтобы приложить к задаче."""
    msg = update.message
    if msg.document:
        name = msg.document.file_name or "файл"
        context.user_data["materials_file"] = {
            "kind": "document", "file_id": msg.document.file_id, "name": name,
        }
        context.user_data["materials"] = f"📎 {name} (приложен ниже)"
    elif msg.photo:
        context.user_data["materials_file"] = {
            "kind": "photo", "file_id": msg.photo[-1].file_id, "name": "фото",
        }
        context.user_data["materials"] = "📎 фото (приложено ниже)"
    else:
        context.user_data["materials"] = "—"
    await msg.reply_text(
        "📎 Принял файл — приложу его к задаче. 🏷️ Теги (например: #excel #клиент #баг).\n\n"
        "Напиши текстом или пропусти:",
        reply_markup=skip_keyboard("tags"),
    )
    return TAGS


async def get_tags(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tags"] = update.message.text.strip()
    await update.message.reply_text("🚦 Статус задачи?", reply_markup=status_keyboard())
    return STATUS


async def skip_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальный обработчик кнопки Пропустить."""
    query = update.callback_query
    await query.answer()
    step = query.data.split("::", 1)[1]

    # двигаемся дальше по цепочке в зависимости от шага
    if step == "dod":
        context.user_data["dod"] = ""
        await query.edit_message_text("🏁 DoD: пропущено")
        await query.message.reply_text("👤 На кого задача?", reply_markup=who_keyboard())
        return WHO
    if step == "deadline":
        context.user_data["deadline"] = "Backlog"
        await query.edit_message_text("🗓️ Дедлайн: Backlog")
        await query.message.reply_text(
            "📋 Что сделать? Перечисли шаги или пропусти:",
            reply_markup=skip_keyboard("steps"),
        )
        return STEPS
    if step == "steps":
        context.user_data["steps"] = ""
        await query.edit_message_text("📋 Шаги: пропущено")
        await query.message.reply_text(
            "📎 Материалы — файл или ссылка?",
            reply_markup=skip_keyboard("materials"),
        )
        return MATERIALS
    if step == "materials":
        context.user_data["materials"] = ""
        await query.edit_message_text("📎 Материалы: пропущено")
        await query.message.reply_text(
            "🏷️ Теги или пропусти:", reply_markup=skip_keyboard("tags")
        )
        return TAGS
    if step == "tags":
        context.user_data["tags"] = ""
        await query.edit_message_text("🏷️ Теги: пропущено")
        await query.message.reply_text("🚦 Статус задачи?", reply_markup=status_keyboard())
        return STATUS
    return ConversationHandler.END


async def publish_task(bot, data):
    """Постит задачу в топик(и) исполнителя и пишет строку в таблицу.

    Возвращает (posted_any: bool, sheet_ok: bool).
    Переиспользуется диалогом /new и голосовым вводом.
    """
    who = data.get("who", "")
    tag_line = ""
    if who == "Лена":
        tag_line = TAG_LENA
    elif who == "Глеб":
        tag_line = TAG_GLEB
    elif who == "Лена + Глеб":
        tag_line = f"{TAG_LENA} {TAG_GLEB}"

    task_text = build_task_text(data)
    if tag_line:
        task_text = f"{tag_line}\n{task_text}"

    # Куда постить:
    # — если заданы топики (THREAD_LENA/THREAD_GLEB) — роутим по исполнителю;
    # — если топиков нет — одна группа без топиков (тег в тексте показывает, на кого задача).
    if THREAD_LENA or THREAD_GLEB:
        if who == "Лена":
            targets = [THREAD_LENA]
        elif who == "Глеб":
            targets = [THREAD_GLEB]
        elif who == "Лена + Глеб":
            targets = [THREAD_LENA, THREAD_GLEB]
        else:
            targets = [None]
    else:
        targets = [None]

    # постим в группу; запоминаем первое сообщение для ссылки
    first_sent = None
    first_thread = None
    for thread_id in targets:
        try:
            sent = await bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=thread_id,
                text=task_text,
                reply_markup=quick_status_keyboard(),
            )
            if first_sent is None:
                first_sent = sent
                first_thread = thread_id
        except Exception as e:
            logger.error("Ошибка постинга (топик %s): %s", thread_id, e)

    # если к задаче приложен файл — кладём его прямо под задачу (в ту же группу/топик)
    file_info = data.get("materials_file")
    if file_info and first_sent is not None:
        try:
            if file_info.get("kind") == "photo":
                await bot.send_photo(
                    chat_id=GROUP_CHAT_ID, message_thread_id=first_thread,
                    photo=file_info["file_id"], caption="📎 Материал к задаче",
                )
            else:
                await bot.send_document(
                    chat_id=GROUP_CHAT_ID, message_thread_id=first_thread,
                    document=file_info["file_id"], caption="📎 Материал к задаче",
                )
        except Exception as e:
            logger.error("Не смог приложить файл к задаче: %s", e)

    link = ""
    if first_sent is not None:
        link = message_link(GROUP_CHAT_ID, first_sent.message_id, first_thread)

    today = datetime.datetime.now(TZINFO).strftime("%Y-%m-%d")
    sheet_ok = append_task_to_sheet(
        today, who, data.get("title", ""), data.get("deadline", ""),
        data.get("status", "🟡 TODO"), link,
    )
    return (first_sent is not None), sheet_ok


async def get_status_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    status = query.data.split("::", 1)[1]
    context.user_data["status"] = status
    await query.edit_message_text(f"🚦 Статус: {status}")

    posted, sheet_ok = await publish_task(context.bot, context.user_data)

    confirm = (
        "✅ Задача создана и отправлена в топик."
        if posted
        else "⚠️ Не удалось запостить в топик."
    )
    confirm += (
        "\n📊 Записана в таблицу."
        if sheet_ok
        else "\n⚠️ В таблицу записать не удалось (проверь доступ)."
    )
    await query.message.reply_text(confirm)

    context.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. Чтобы начать заново — /new")
    return ConversationHandler.END


async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/purge — запрашивает подтверждение перед удалением всех задач из таблицы."""
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}" if SHEET_ID else "таблица"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, удалить всё", callback_data="purge::confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="purge::cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ Это удалит ВСЕ задачи из Google Sheets.\n"
        "Сообщения в Telegram останутся, только таблица будет очищена.\n\n"
        "Продолжить?",
        reply_markup=keyboard,
    )


async def on_purge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок подтверждения /purge."""
    query = update.callback_query
    await query.answer()
    action = query.data.split("::", 1)[1]
    if action == "cancel":
        await query.edit_message_text("Отменено — задачи не тронуты.")
        return
    await query.edit_message_text("🗑 Удаляю все задачи…")
    count = await asyncio.to_thread(purge_all_tasks)
    if count == -1:
        await query.edit_message_text("⚠️ Ошибка при очистке таблицы.")
    elif count == 0:
        await query.edit_message_text("Таблица уже пуста — нечего удалять.")
    else:
        await query.edit_message_text(f"✅ Удалено задач: {count}. Таблица очищена.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — показать открытые задачи (статус ≠ DONE/CANCELLED), сгруппированные по исполнителю."""
    try:
        # gspread синхронный — уводим чтение в поток, чтобы не блокировать бота
        groups = await asyncio.to_thread(read_open_tasks)
    except Exception as e:
        logger.error("Ошибка чтения таблицы для /list: %s", e)
        await update.message.reply_text(
            "⚠️ Не получилось прочитать таблицу. Попробуй ещё раз чуть позже."
        )
        return

    if not groups:
        await update.message.reply_text("🎉 Открытых задач нет — всё закрыто!")
        return

    # порядок групп: сначала Лена, Глеб, Лена + Глеб, затем прочие
    order = {who: i for i, who in enumerate(WHO_OPTIONS)}
    lines = ["📋 ОТКРЫТЫЕ ЗАДАЧИ", ""]
    total = 0
    for who in sorted(groups, key=lambda w: (order.get(w, 99), w)):
        tasks = groups[who]
        total += len(tasks)
        lines.append(f"👤 {who} — {len(tasks)}")
        for t in tasks:
            title = t["title"] or "(без названия)"
            lines.append(f"   {t['status']}  {title}  ·  🗓️ {t['deadline']}")
        lines.append("")
    lines.append(f"Итого открытых: {total}")
    await update.message.reply_text("\n".join(lines).strip())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status в ответ на опубликованную задачу — меняет её статус (в таблице и в самом сообщении)."""
    msg = update.message
    replied = msg.reply_to_message
    text = (replied.text or replied.caption) if replied else None
    if not text:
        await msg.reply_text(
            "Ответь командой /status на сообщение с задачей — тогда поменяю её статус."
        )
        return
    title = extract_task_title(text)
    if not title:
        await msg.reply_text("Это сообщение не похоже на задачу (нет строки «📌 ЗАДАЧА: …»).")
        return
    # запоминаем, какую задачу и какое сообщение редактируем
    context.user_data["status_edit"] = {
        "title": title,
        "chat_id": replied.chat_id,
        "message_id": replied.message_id,
        "orig_text": text,
    }
    await msg.reply_text(
        f"🚦 Новый статус для задачи:\n«{title}»",
        reply_markup=status_keyboard("setstatus"),
    )


async def on_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка смены статуса — обновляем строку в таблице и текст задачи."""
    query = update.callback_query
    await query.answer()
    new_status = query.data.split("::", 1)[1]

    info = context.user_data.get("status_edit")
    if not info:
        await query.edit_message_text(
            "⏳ Запрос устарел. Ответь на задачу командой /status ещё раз."
        )
        return

    # 1) обновляем строку в таблице
    try:
        found = await asyncio.to_thread(update_task_status, info["title"], new_status)
        sheet_result = "ok" if found else "notfound"
    except Exception as e:
        logger.error("Ошибка обновления статуса в таблице: %s", e)
        sheet_result = "error"

    # 2) обновляем само сообщение с задачей (меняем строку статуса)
    edited = False
    new_text = replace_status_line(info["orig_text"], new_status)
    if new_text and new_text != info["orig_text"]:
        try:
            await context.bot.edit_message_text(
                chat_id=info["chat_id"],
                message_id=info["message_id"],
                text=new_text,
            )
            edited = True
        except Exception as e:
            logger.error("Не смог отредактировать сообщение задачи: %s", e)

    # 3) собираем ответ пользователю
    where = []
    if edited:
        where.append("в задаче")
    if sheet_result == "ok":
        where.append("в таблице")
    head = (
        f"✅ Статус обновлён ({' и '.join(where)}): {new_status}"
        if where
        else f"🚦 Статус: {new_status}"
    )
    tail = ""
    if sheet_result == "notfound":
        tail = "\n⚠️ Строку в таблице не нашёл — этой задачи там нет."
    elif sheet_result == "error":
        tail = "\n⚠️ В таблицу записать не вышло (Google недоступен)."
    await query.edit_message_text(head + tail)

    context.user_data.pop("status_edit", None)


async def on_quick_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка статуса под опубликованной задачей — меняем статус в один тап."""
    query = update.callback_query
    code = query.data.split("::", 1)[1]
    new_status = STATUS_BY_CODE.get(code)
    if not new_status:
        await query.answer("Неизвестный статус")
        return

    # сама задача — это текст сообщения с кнопкой, берём название оттуда
    text = query.message.text or query.message.caption or ""
    title = extract_task_title(text)
    if not title:
        await query.answer("Не вижу задачу в этом сообщении", show_alert=True)
        return

    # 1) обновляем строку в таблице
    try:
        found = await asyncio.to_thread(update_task_status, title, new_status)
        sheet_result = "ok" if found else "notfound"
    except Exception as e:
        logger.error("Ошибка смены статуса кнопкой: %s", e)
        sheet_result = "error"

    # 2) обновляем текст задачи (кнопки оставляем на месте).
    #    Если статус уже такой — текст не меняем, чтобы не ловить "message is not modified".
    new_text = replace_status_line(text, new_status)
    if new_text and new_text != text:
        try:
            await query.edit_message_text(new_text, reply_markup=quick_status_keyboard())
        except Exception as e:
            logger.error("Не смог обновить сообщение задачи кнопкой: %s", e)

    # 3) короткий тост пользователю
    if sheet_result == "ok":
        await query.answer(f"Статус: {new_status}")
    elif sheet_result == "notfound":
        await query.answer("Обновил в сообщении, но в таблице задачи нет", show_alert=True)
    else:
        await query.answer("Таблица недоступна — обновил только сообщение", show_alert=True)


async def build_and_send_digest(bot):
    """Собирает дайджест дедлайнов на сегодня и шлёт в General.
    Возвращает число задач, или -1 при ошибке чтения таблицы."""
    try:
        due = await asyncio.to_thread(read_due_today)
    except Exception as e:
        logger.error("Дайджест: не смог прочитать таблицу: %s", e)
        return -1
    today_str = datetime.datetime.now(TZINFO).strftime("%d.%m")
    if not due:
        return 0
    lines = [f"☀️ Доброе утро! Дедлайн сегодня — {today_str}:", ""]
    for t in due:
        lines.append(f"   {t['status']}  {t['title']} — 👤 {t['who']}")
    lines.append("")
    lines.append(f"{TAG_LENA} {TAG_GLEB}")
    # General — отправка без message_thread_id
    await bot.send_message(chat_id=GROUP_CHAT_ID, text="\n".join(lines))
    return len(due)


async def send_digest(context: ContextTypes.DEFAULT_TYPE):
    """Колбэк ежедневного задания JobQueue."""
    n = await build_and_send_digest(context.bot)
    if n == 0:
        logger.info("Дайджест: на сегодня задач с дедлайном нет — не отправляю")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/digest — ручной запуск дайджеста (удобно проверить после деплоя)."""
    n = await build_and_send_digest(context.bot)
    if n == -1:
        await update.message.reply_text("⚠️ Не смог прочитать таблицу. Попробуй позже.")
    elif n == 0:
        await update.message.reply_text("На сегодня задач с дедлайном нет 🎉")
    else:
        await update.message.reply_text(f"📨 Отправил дайджест в группу. Задач сегодня — {n}.")


# маршрутизация алерта по исполнителю: (топик, тег)
def _alert_targets(who):
    if who == "Лена":
        return [(THREAD_LENA, TAG_LENA)]
    if who == "Глеб":
        return [(THREAD_GLEB, TAG_GLEB)]
    if who == "Лена + Глеб":
        return [(THREAD_LENA, TAG_LENA), (THREAD_GLEB, TAG_GLEB)]
    return []


async def build_and_send_alerts(bot):
    """Шлёт алерты по задачам с дедлайном = завтра, тегая исполнителя в его топике.
    Возвращает число отправленных сообщений, или -1 при ошибке чтения."""
    tomorrow = (datetime.datetime.now(TZINFO) + datetime.timedelta(days=1)).strftime("%d.%m")
    try:
        due = await asyncio.to_thread(read_tasks_due, tomorrow)
    except Exception as e:
        logger.error("Алерты: не смог прочитать таблицу: %s", e)
        return -1
    count = 0
    for t in due:
        targets = _alert_targets(t["who"])
        if not targets:
            continue  # неизвестный исполнитель — пропускаем
        body = f"⏰ Дедлайн завтра ({tomorrow})\n{t['status']}  {t['title']}"
        for thread_id, tag in targets:
            try:
                await bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    message_thread_id=thread_id,
                    text=f"{tag}\n{body}",
                    reply_markup=_alert_keyboard(t["title"]),
                )
                count += 1
            except Exception as e:
                logger.error("Алерт: не смог отправить в топик %s: %s", thread_id, e)
    return count


def _alert_keyboard(title: str) -> InlineKeyboardMarkup:
    """Кнопки под дедлайн-алертом: быстро сменить статус или перенести."""
    safe = title[:40]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Готово", callback_data=f"alert::done::{safe}"),
            InlineKeyboardButton("🔵 В работу", callback_data=f"alert::wip::{safe}"),
        ],
        [InlineKeyboardButton("📅 Перенести на день", callback_data=f"alert::postpone::{safe}")],
    ])


async def on_alert_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки под дедлайн-алертом: done / wip / postpone."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("::", 2)
    action, title = parts[1], parts[2]
    if action == "done":
        ok = await asyncio.to_thread(update_task_status, title, "🟢 DONE")
        await query.edit_message_text(
            query.message.text + "\n\n✅ Статус обновлён → DONE" if ok
            else query.message.text + "\n\n⚠️ Задача не найдена в таблице."
        )
    elif action == "wip":
        ok = await asyncio.to_thread(update_task_status, title, "🔵 WIP")
        await query.edit_message_text(
            query.message.text + "\n\n🔵 Статус обновлён → В работе" if ok
            else query.message.text + "\n\n⚠️ Задача не найдена."
        )
    elif action == "postpone":
        try:
            ws = get_worksheet()
            records = await asyncio.to_thread(ws.get_all_records)
            tomorrow = (datetime.datetime.now(TZINFO) + datetime.timedelta(days=2)).strftime("%d.%m")
            for idx, row in enumerate(records):
                if str(row.get("Задача", "")).strip().startswith(title):
                    await asyncio.to_thread(ws.update_cell, idx + 2, 4, tomorrow)
                    await query.edit_message_text(
                        query.message.text + f"\n\n📅 Дедлайн перенесён на {tomorrow}"
                    )
                    return
            await query.edit_message_text(query.message.text + "\n\n⚠️ Задача не найдена.")
        except Exception as e:
            logger.error("alert postpone: %s", e)
            await query.edit_message_text(query.message.text + "\n\n⚠️ Ошибка при переносе.")


async def send_deadline_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Колбэк ежедневного задания JobQueue."""
    n = await build_and_send_alerts(context.bot)
    if n == 0:
        logger.info("Алерты: на завтра дедлайнов нет")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alerts — ручной запуск дедлайн-алертов (для проверки)."""
    n = await build_and_send_alerts(context.bot)
    if n == -1:
        await update.message.reply_text("⚠️ Не смог прочитать таблицу. Попробуй позже.")
    elif n == 0:
        await update.message.reply_text("На завтра дедлайнов нет 👍")
    else:
        await update.message.reply_text(f"⏰ Разослал алерты по задачам на завтра: {n}.")


# ------------------------------------------------------------------
# ГОЛОСОВОЙ ВВОД (#6): голос → Whisper → GPT → черновик задачи
# ------------------------------------------------------------------
VOICE_ROUTER_PROMPT = (
    "Ты — роутер голосовых команд для Pastila OS (Лена + Глеб, бизнес по пастиле). "
    "Тебе дают транскрипт голосового. Определи намерение и верни СТРОГО JSON — один из шести.\n\n"

    "1. СОЗДАТЬ ЗАДАЧУ — только если явно просят «поставь задачу», «создай задачу», "
    "«запиши поручение», «добавь задачу»:\n"
    '{"type":"task","title":"...","dod":"","who":"","deadline":"","steps":[],"materials":"","tags":[],"status":"NEW"}\n'
    "who — «Лена»/«Глеб»/«Лена + Глеб»/\"\"; deadline — ДД.ММ или \"\" (относительные от сегодня).\n\n"

    "2. ПОКАЗАТЬ ЗАДАЧИ — «покажи задачи», «что в работе», «список задач», «что у Глеба/Лены»:\n"
    '{"type":"list","who":"Лена"|"Глеб"|"Лена + Глеб"|""}\n'
    "who — если спрашивают про конкретного, иначе \"\" (все).\n\n"

    "3. ПЛАН РАБОТЫ — «составь план», «что делать Глебу/Лене», «наш план», «расставь приоритеты в задачах»:\n"
    '{"type":"plan","who":"Лена"|"Глеб"|"Лена + Глеб","extra":""}\n'
    "extra — если добавили контекст («к запуску», «на эту неделю» и т.п.).\n\n"

    "4. НАЙТИ ЗАДАЧИ В ПЕРЕПИСКЕ — «найди задачи в чате», «что мы обсуждали сделать», «задачи из переписки»:\n"
    '{"type":"analyze"}\n\n'

    "5. ДЕДЛАЙНЫ — «что сдавать сегодня/завтра», «дедлайны», «напомни о сроках»:\n"
    '{"type":"digest"}\n\n'

    "6. ЛЮБОЙ ДРУГОЙ ЗАПРОС — вопрос, анализ, совет, мнение, «кто прав», «что важнее», "
    "«направление бизнеса», «как лучше», поиск по переписке, и вообще всё что не вошло выше:\n"
    '{"type":"query","clean_text":"<грамотно переформулированный запрос, исправь ошибки транскрипции>"}\n\n'

    "Если сомневаешься — выбирай query. "
    "В clean_text — читабельная грамотная формулировка того, что хочет пользователь."
)

VOICE_ANALYSIS_PROMPT = (
    "Ты — старший бизнес-советник команды Pastila OS "
    "(производство и продажа белёвской пастилы, Тульская область).\n"
    "Глеб (@foxruso) — контент, коммуникации, стратегия, шеф.\n"
    "Лена (@elenaisanewleet) — разработка, техника, продукт.\n\n"

    "Что ты умеешь:\n"
    "· Анализировать бизнес-ситуацию и давать конкретные рекомендации с обоснованием\n"
    "· Определять приоритеты — что важнее всего прямо сейчас и почему\n"
    "· Честно оценивать действия, подход или логику (не лизать, а помогать)\n"
    "· Выявлять скрытые риски и узкие места\n"
    "· Давать своё мнение уверенно — тебя за это и спрашивают\n"
    "· Цитировать переписку точно: Имя: «цитата»\n\n"

    "Если в ответе ты замечаешь очевидно лучший способ сделать что-то — "
    "добавь в конце блок:\n"
    "💡 Совет: [одно конкретное предложение как улучшить]\n\n"

    "Правила ответа:\n"
    "· Конкретно и по делу, без вступлений и воды\n"
    "· Если спрашивают мнение — дай его чётко, с аргументами\n"
    "· Обычный текст, без markdown-звёздочек\n"
    "· Если данных не хватает — скажи честно что именно не видно"
)


async def _whisper_transcribe(audio_bytes):  # OpenAI Whisper — только для голоса
    """Распознаёт речь через OpenAI Whisper. Возвращает текст."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    files = {"file": ("voice.ogg", audio_bytes, "audio/ogg")}
    data = {"model": WHISPER_MODEL}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers, files=files, data=data,
        )
        r.raise_for_status()
        return r.json().get("text", "")


async def _gpt_voice_route(transcript, chat_id):
    """Роутер: определяет намерение и очищает транскрипт.
    Возвращает {"type":"task",...} или {"type":"query","clean_text":"..."}."""
    now = datetime.datetime.now(TZINFO)
    model_id = llm.model_id_for(chat_id, "voice_route", transcript)
    content = await llm.call_llm(
        [{"role": "system", "content": VOICE_ROUTER_PROMPT},
         {"role": "user", "content": f"Сегодня {now:%d.%m.%Y}.\n\nГолосовое: {transcript}"}],
        model_id, temperature=0, max_tokens=400,
        response_format={"type": "json_object"},
    )
    return llm.loads_loose(content)


async def _gpt_voice_analyze(clean_text, chat_log, chat_id):
    """Аналитический ответ на произвольный запрос по переписке и контексту бизнеса."""
    now = datetime.datetime.now(TZINFO)
    log_text = await llm.prepare_context(chat_log, chat_id, task="voice_route")
    model_id = llm.model_id_for(chat_id, "voice_route", log_text + " " + clean_text)
    kb = _kb_context()
    static_ctx = f"Сегодня {now:%d.%m.%Y}.\n\nПереписка команды:\n{log_text}"
    if kb:
        static_ctx += f"\n\nБаза знаний:\n{kb}"
    return await llm.call_llm(
        [llm.sys_cached(VOICE_ANALYSIS_PROMPT),
         llm.user_with_cache(static_ctx, f"Запрос: {clean_text}")],
        model_id, temperature=0.3, max_tokens=900,
    )


def _draft_from_parsed(parsed):
    """Превращает разобранный JSON в данные задачи (как в диалоге /new)."""
    who = parsed.get("who", "")
    if who not in ("Лена", "Глеб", "Лена + Глеб"):
        who = "Лена + Глеб"  # не понял исполнителя — покажем обоим, поправят перед публикацией

    deadline = parse_deadline(str(parsed.get("deadline", ""))) or "Backlog"

    steps_val = parsed.get("steps") or []
    if isinstance(steps_val, str):
        steps_val = [steps_val]
    steps = "\n".join(str(s).strip() for s in steps_val if str(s).strip())

    tags_val = parsed.get("tags") or []
    if isinstance(tags_val, str):
        tags_val = re.split(r"[\s,]+", tags_val)
    tags = " ".join("#" + str(t).lstrip("#") for t in tags_val if str(t).strip())

    status = STATUS_BY_CODE.get(str(parsed.get("status", "NEW")).upper(), "⚪️ NEW")

    return {
        "title": str(parsed.get("title", "")).strip() or "(без названия)",
        "dod": str(parsed.get("dod", "")).strip(),
        "who": who,
        "deadline": deadline,
        "steps": steps,
        "materials": str(parsed.get("materials", "")).strip(),
        "tags": tags,
        "status": status,
    }


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовое → Whisper → роутер → задача или аналитический ответ."""
    # если ждём аудио-контент сессии — перехватываем
    if await _handle_session_file(update, context):
        return
    if not OPENAI_API_KEY:
        return  # нет Whisper — молчим
    if not llm.OPENROUTER_API_KEY:
        await update.message.reply_text(
            "🔇 Голосовой ввод выключен — не задан OPENROUTER_API_KEY (нужен для разбора)."
        )
        return
    msg = update.message
    status_msg = await msg.reply_text("🎙️ Распознаю…")
    try:
        tg_file = await msg.voice.get_file()
        audio = await tg_file.download_as_bytearray()
        transcript = await _whisper_transcribe(bytes(audio))
        if not transcript.strip():
            await status_msg.edit_text("🤔 Не разобрал речь. Попробуй ещё раз или /new.")
            return
        # длинное голосовое (>50 слов) → структурированный разбор вместо роутинга
        if len(transcript.split()) > 50:
            await _handle_long_voice(msg, transcript, status_msg)
            return
        result = await _gpt_voice_route(transcript, msg.chat_id)
    except Exception as e:
        logger.error("Голос: ошибка роутинга: %s", e)
        await status_msg.edit_text("⚠️ Не получилось обработать голосовое. Попробуй ещё раз.")
        return

    vtype = result.get("type", "query")

    # ── Показать задачи ──
    if vtype == "list":
        await status_msg.edit_text("📋 Читаю таблицу…")
        try:
            groups = await asyncio.to_thread(read_open_tasks)
        except Exception as e:
            logger.error("Голос/list: %s", e)
            await status_msg.edit_text("⚠️ Не получилось прочитать таблицу.")
            return
        who_filter = result.get("who", "")
        if who_filter and who_filter in groups:
            groups = {who_filter: groups[who_filter]}
        elif who_filter:
            # попробуем нечёткий матч (Лена + Глеб содержит обоих)
            groups = {w: t for w, t in groups.items() if _person_match(w, who_filter)}
        order = {w: i for i, w in enumerate(WHO_OPTIONS)}
        lines = ["📋 ОТКРЫТЫЕ ЗАДАЧИ" + (f" — {who_filter}" if who_filter else ""), ""]
        total = 0
        for who in sorted(groups, key=lambda w: (order.get(w, 99), w)):
            tasks = groups[who]
            total += len(tasks)
            lines.append(f"👤 {who} — {len(tasks)}")
            for t in tasks:
                lines.append(f"   {t['status']}  {t['title']}  ·  🗓️ {t['deadline']}")
            lines.append("")
        lines.append(f"Итого: {total}" if total else "Открытых задач нет 🎉")
        await status_msg.edit_text("\n".join(lines).strip())
        return

    # ── План работы ──
    if vtype == "plan":
        who = result.get("who") or "Лена + Глеб"
        extra = result.get("extra") or ""
        buf = _CHAT_LOG.get(msg.chat_id, [])
        transcript_ctx = await llm.prepare_context(buf, msg.chat_id, task="plan")
        model_id, label, is_auto = llm.resolve_model(msg.chat_id, "plan", transcript_ctx)
        await status_msg.edit_text(
            f"🗂 Составляю план для «{who}» ({label}{' · авто' if is_auto else ''})…"
        )
        try:
            plan = (await _gpt_plan(transcript_ctx, extra, model_id)) if who == "Лена + Глеб" \
                else (await _gpt_plan_person(who, transcript_ctx, model_id))
        except Exception as e:
            logger.error("Голос/план: %s", e)
            await status_msg.edit_text("⚠️ Не получилось составить план. Попробуй позже.")
            return
        chunks = _chunks(plan.strip() or "Пусто.")
        await status_msg.edit_text(chunks[0])
        for ch in chunks[1:]:
            await msg.reply_text(ch)
        return

    # ── Анализ переписки на задачи ──
    if vtype == "analyze":
        buf = _CHAT_LOG.get(msg.chat_id, [])
        if len(buf) < 3:
            await status_msg.edit_text(
                "Мало сообщений для анализа — бот видит переписку только с момента запуска."
            )
            return
        transcript_ctx = await llm.prepare_context(buf, msg.chat_id, task="analyze")
        model_id, label, is_auto = llm.resolve_model(msg.chat_id, "analyze", transcript_ctx)
        await status_msg.edit_text(
            f"🧠 Ищу задачи в переписке ({label}{' · авто' if is_auto else ''})…"
        )
        try:
            tasks = await _gpt_analyze(transcript_ctx, model_id)
        except Exception as e:
            logger.error("Голос/analyze: %s", e)
            await status_msg.edit_text("⚠️ Не получилось проанализировать.")
            return
        if not tasks:
            await status_msg.edit_text("Конкретных задач в переписке не нашёл 🤷")
            return
        await status_msg.edit_text(
            f"Нашёл задач: {len(tasks)}. Выбери статус, чтобы завести (или «Пропустить»):"
        )
        for parsed in tasks[:5]:
            data = _draft_from_parsed(parsed)
            draft = f"— Черновик из переписки —\n{build_task_text(data)}\n\n🚦 Статус:"
            rows = [list(r) for r in status_keyboard("voicestatus").inline_keyboard]
            rows.append([InlineKeyboardButton("❌ Пропустить", callback_data="voice::cancel")])
            sent = await context.bot.send_message(
                chat_id=msg.chat_id, message_thread_id=msg.message_thread_id,
                text=draft, reply_markup=InlineKeyboardMarkup(rows),
            )
            context.chat_data.setdefault("voice_drafts", {})[sent.message_id] = data
        return

    # ── Дедлайны ──
    if vtype == "digest":
        await status_msg.edit_text("📅 Смотрю дедлайны…")
        n = await build_and_send_digest(context.bot)
        if n == -1:
            await status_msg.edit_text("⚠️ Не смог прочитать таблицу.")
        elif n == 0:
            await status_msg.edit_text("На сегодня дедлайнов нет 🎉")
        else:
            await status_msg.edit_text(f"📨 Отправил дайджест в группу. Задач сегодня — {n}.")
        return

    # ── Создание задачи ──
    if vtype == "task":
        try:
            data = _draft_from_parsed(result)
        except Exception as e:
            logger.error("Голос: ошибка разбора задачи: %s", e)
            await status_msg.edit_text("⚠️ Не смог разобрать задачу. Заведи через /new.")
            return
        preview = transcript.strip()
        preview = preview if len(preview) <= 200 else preview[:200] + "…"
        draft = (
            f"🎙️ «{preview}»\n\n— Черновик задачи —\n{build_task_text(data)}\n\n"
            "🚦 Выбери статус — и задача опубликуется:"
        )
        rows = [list(row) for row in status_keyboard("voicestatus").inline_keyboard]
        rows.append([InlineKeyboardButton("❌ Отмена", callback_data="voice::cancel")])
        sent = await status_msg.edit_text(draft, reply_markup=InlineKeyboardMarkup(rows))
        context.chat_data.setdefault("voice_drafts", {})[sent.message_id] = data
        return

    # ── Аналитический запрос (query + всё неопознанное) ──
    clean_text = (result.get("clean_text") or transcript).strip()
    await status_msg.edit_text(f"🎙️ «{clean_text}»\n\n⏳ Думаю…")
    try:
        chat_log = _CHAT_LOG.get(msg.chat_id, [])
        answer = await _gpt_voice_analyze(clean_text, chat_log, msg.chat_id)
        answer = answer.strip() or "Не получилось найти ответ."
    except Exception as e:
        logger.error("Голос/query: %s", e)
        await status_msg.edit_text(
            f"🎙️ «{clean_text}»\n\n⚠️ Не получилось проанализировать. Попробуй позже."
        )
        return
    header = f"🎙️ «{clean_text}»\n\n"
    full = header + answer
    if len(full) <= 4000:
        await status_msg.edit_text(full)
    else:
        await status_msg.edit_text(header + answer[:4000 - len(header)].rstrip() + "…")


async def on_voice_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки под голосовым черновиком: опубликовать / отмена."""
    query = update.callback_query
    await query.answer()
    action = query.data.split("::", 1)[1]
    data = context.chat_data.get("voice_drafts", {}).pop(query.message.message_id, None)

    if data is None:
        await query.edit_message_text("⏳ Черновик устарел. Запиши голосовое заново.")
        return
    if action == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return

    await query.edit_message_text("⏳ Публикую…")
    posted, sheet_ok = await publish_task(context.bot, data)
    confirm = "✅ Задача опубликована." if posted else "⚠️ Не удалось запостить в топик."
    confirm += "\n📊 Записана в таблицу." if sheet_ok else "\n⚠️ В таблицу не записал (проверь доступ)."
    await query.edit_message_text(confirm)


async def on_voice_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Под голосовым черновиком выбран статус — публикуем задачу с ним."""
    query = update.callback_query
    await query.answer()
    status = query.data.split("::", 1)[1]
    data = context.chat_data.get("voice_drafts", {}).pop(query.message.message_id, None)
    if data is None:
        await query.edit_message_text("⏳ Черновик устарел. Запиши голосовое заново.")
        return
    data["status"] = status
    await query.edit_message_text(f"🚦 Статус: {status}\n⏳ Публикую…")
    posted, sheet_ok = await publish_task(context.bot, data)
    confirm = "✅ Задача опубликована." if posted else "⚠️ Не удалось запостить в топик."
    confirm += "\n📊 Записана в таблицу." if sheet_ok else "\n⚠️ В таблицу не записал (проверь доступ)."
    await query.edit_message_text(confirm)


# ------------------------------------------------------------------
# АНАЛИЗ ПЕРЕПИСКИ (вариант B): бот копит сообщения и по /analyze предлагает задачи
# ------------------------------------------------------------------
_CHAT_LOG = {}   # chat_id -> [(имя, текст), ...] в памяти (сбрасывается при перезапуске бота)
_LOG_MAX = 2000  # сколько последних сообщений держим на чат (с запасом под импорт истории)

# База знаний — файлы и страницы Notion. Хранится в Sheet «_knowledge» и грузится при старте.
_KNOWLEDGE: list[dict] = []      # [{source, item_id, title, content, added_at}, ...]
_KNOWLEDGE_IDS: set[str] = set() # для дедупликации по item_id


async def on_log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тихо копит текстовые сообщения чата. Если ждём сессию — перехватывает."""
    msg = update.message
    if not msg or not msg.text or msg.text.startswith("/"):
        return
    # сначала проверяем: ждём ли контент сессии
    if await _handle_session_text(update, context):
        return
    name = msg.from_user.full_name if msg.from_user else "?"
    buf = _CHAT_LOG.setdefault(msg.chat_id, [])
    buf.append((name, msg.text))
    if len(buf) > _LOG_MAX:
        del buf[: len(buf) - _LOG_MAX]
    # фоновые задачи — не блокируют ответ
    asyncio.create_task(
        _maybe_check_insight(msg.chat_id, msg.message_id, msg.text, context.bot)
    )
    asyncio.create_task(
        _check_triggers(msg.chat_id, msg.text, context.bot)
    )


# ------------------------------------------------------------------
# ДЕТЕКТОР ВАЖНЫХ МЫСЛЕЙ — авто-реакция ⭐ + теги + KB
# ------------------------------------------------------------------

INSIGHT_PROMPT = (
    "Ты — аналитик команды Pastila OS (белёвская пастила, Лена + Глеб).\n"
    "Посмотри на последние сообщения. Найди ОДНУ — самую ценную мысль, если она есть.\n\n"
    "ЧТО ВЫДЕЛЯТЬ:\n"
    "• стратегическая идея или инсайт про бизнес\n"
    "• важное решение, влияющее на продукт/процесс/команду\n"
    "• открытие про клиентов, рынок, конкурентов\n"
    "• чёткая формулировка ключевой проблемы или возможности\n"
    "• риск или ограничение которое важно помнить\n\n"
    "ЧТО НЕ ВЫДЕЛЯТЬ:\n"
    "• обычная координация («сделай», «ок», «когда будет готово»)\n"
    "• задачи и поручения (для них есть /new)\n"
    "• светская беседа\n\n"
    "Верни СТРОГО JSON:\n"
    '{"found": false}  — если ничего действительно ценного нет\n'
    '{"found": true, "author": "имя", "quote": "точная цитата", '
    '"why": "одно предложение почему важно", '
    '"tags": ["тег1", "тег2"]}  — до 3 тегов из: идея/решение/стратегия/риск/клиенты/продукт/партнёры/финансы/процесс\n\n'
    "Будь ИЗБИРАТЕЛЬНЫМ. Если сомневаешься — {\"found\": false}."
)

_INSIGHT_COUNTER: dict[int, int] = {}   # сообщений с последней проверки
_INSIGHT_LAST: dict[int, float] = {}    # timestamp последнего найденного инсайта
_INSIGHT_COOLDOWN = 900.0               # минимум 15 мин между инсайтами на чат
_INSIGHT_CHECK_EVERY = 7                # проверять каждые N сообщений
_INSIGHT_MIN_LEN = 90                   # или если сообщение длиннее этого


async def _maybe_check_insight(chat_id: int, msg_id: int, msg_text: str, bot) -> None:
    """Вызывается из on_log_message. Периодически ищет важные мысли в последних сообщениях."""
    if not llm.OPENROUTER_API_KEY:
        return

    # счётчик и порог
    count = _INSIGHT_COUNTER.get(chat_id, 0) + 1
    _INSIGHT_COUNTER[chat_id] = count
    long_enough = len(msg_text) >= _INSIGHT_MIN_LEN
    if not long_enough and count < _INSIGHT_CHECK_EVERY:
        return
    _INSIGHT_COUNTER[chat_id] = 0

    # cooldown
    import time as _time
    if _time.monotonic() - _INSIGHT_LAST.get(chat_id, 0.0) < _INSIGHT_COOLDOWN:
        return

    buf = _CHAT_LOG.get(chat_id, [])
    if len(buf) < 3:
        return

    recent = buf[-10:]
    transcript = "\n".join(f"{name}: {text}" for name, text in recent)

    try:
        raw = await llm.call_llm(
            [llm.sys_cached(INSIGHT_PROMPT),
             {"role": "user", "content": transcript}],
            "haiku45", temperature=0, max_tokens=250,
            response_format={"type": "json_object"},
        )
        result = llm.loads_loose(raw)
    except Exception as e:
        logger.debug("insight check failed: %s", e)
        return

    if not isinstance(result, dict) or not result.get("found"):
        return

    import time as _time
    _INSIGHT_LAST[chat_id] = _time.monotonic()

    author = result.get("author", "")
    quote = result.get("quote", "").strip()
    why = result.get("why", "").strip()
    tags = result.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    if not quote:
        return

    # ⭐ реакция на последнее сообщение
    try:
        await bot.set_message_reaction(chat_id, msg_id, [ReactionTypeEmoji(emoji="⭐")])
    except Exception:
        pass

    # сохранить в KB
    item_id = f"insight_{hashlib.md5(quote.encode()).hexdigest()[:12]}"
    full_title = f"Инсайт [{', '.join(tags) if tags else 'общее'}]: {quote[:60]}"
    content_kb = f"{author}: «{quote}»\n\nПочему важно: {why}"
    await asyncio.to_thread(_add_knowledge_item, "insight", item_id, full_title, content_kb, tags)

    # уведомление в чат (если включено)
    if INSIGHT_NOTIFY:
        tags_line = "  " + " ".join(f"#{t}" for t in tags) if tags else ""
        await bot.send_message(
            chat_id,
            f"⭐ Сохранила в базу знаний.\n\n"
            f"«{quote[:120]}{'…' if len(quote) > 120 else ''}»\n\n"
            f"{why}{tags_line}",
        )


# ------------------------------------------------------------------
# БАЗА ЗНАНИЙ — извлечение контента из файлов
# ------------------------------------------------------------------

def _extract_pdf(data: bytes) -> str:
    if not _PDF_OK:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n".join(p for p in pages if p.strip())[:6000]
    except Exception as e:
        logger.error("PDF extract: %s", e)
        return ""


def _extract_docx(data: bytes) -> str:
    if not _DOCX_OK:
        return ""
    try:
        doc = DocxDocument(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())[:6000]
    except Exception as e:
        logger.error("DOCX extract: %s", e)
        return ""


async def _vision_describe(image_bytes: bytes, filename: str = "") -> str:
    """Описывает изображение через vision-модель (OpenRouter)."""
    if not llm.OPENROUTER_API_KEY:
        return ""
    try:
        b64 = base64.b64encode(image_bytes).decode()
        note = f" ({filename})" if filename else ""
        messages = [
            {"role": "system", "content": (
                "Ты помощник команды Pastila OS (пастила-бизнес). "
                "Подробно опиши, что видишь на изображении. "
                "Если это схема, диаграмма, макет или документ — разбери структуру и ключевые данные. "
                "Если это фото продукта или места — опиши предметно. Отвечай по-русски."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": f"Опиши это изображение{note}:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ]
        return await llm.call_llm(messages, VISION_MODEL_KEY, temperature=0.2, max_tokens=600)
    except Exception as e:
        logger.error("vision_describe: %s", e)
        return ""


async def _summarize_content(content: str, title: str) -> str:
    """Краткое саммари загруженного документа/сессии через LLM."""
    if not llm.OPENROUTER_API_KEY or len(content) < 100:
        return ""
    try:
        prompt = (
            "Дай краткое саммари этого документа в 3-5 предложениях. "
            "Выдели ключевые идеи, решения или выводы. Только суть, без воды. "
            "Отвечай по-русски."
        )
        snippet = content[:4000]
        return await llm.call_llm(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": f"Документ: «{title}»\n\n{snippet}"}],
            "haiku45", temperature=0.2, max_tokens=350,
        )
    except Exception as e:
        logger.debug("summarize: %s", e)
        return ""


async def _download_tg_file(bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Авто-обработка файлов/фото в группе → извлечение текста → база знаний."""
    # если ждём контент сессии — передаём туда
    if await _handle_session_file(update, context):
        return
    if not llm.OPENROUTER_API_KEY and not _PDF_OK and not _DOCX_OK:
        return
    msg = update.message
    if not msg:
        return

    # Определяем что пришло
    doc = msg.document
    photo = msg.photo[-1] if msg.photo else None

    if not doc and not photo:
        return

    # Имя, mime, file_id
    if doc:
        file_id = doc.file_id
        filename = doc.file_name or ""
        mime = (doc.mime_type or "").lower()
        size = doc.file_size or 0
    else:
        file_id = photo.file_id
        filename = "photo.jpg"
        mime = "image/jpeg"
        size = photo.file_size or 0

    # Пропускаем > 20 МБ (Telegram limit) и уже виденные
    if size > 20 * 1024 * 1024:
        return
    item_id = file_id  # Telegram file_id стабилен для одного файла
    if item_id in _KNOWLEDGE_IDS:
        return

    title = filename or "photo"
    status_msg = await msg.reply_text(f"📎 Читаю «{title}»…")

    try:
        data = await _download_tg_file(context.bot, file_id)
        content = ""

        if mime == "application/pdf" or filename.lower().endswith(".pdf"):
            content = await asyncio.to_thread(_extract_pdf, data)
            label = "PDF"

        elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",) \
                or filename.lower().endswith(".docx"):
            content = await asyncio.to_thread(_extract_docx, data)
            label = "DOCX"

        elif mime.startswith("image/") or filename.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            content = await _vision_describe(data, filename)
            label = "Изображение"

        elif mime.startswith("audio/") or mime == "video/ogg" \
                or filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a")):
            if OPENAI_API_KEY:
                content = await _whisper_transcribe(data)
                label = "Аудио"
            else:
                await status_msg.edit_text("🔇 Аудио: нет OPENAI_API_KEY для расшифровки.")
                return

        elif mime in (
            "text/plain", "text/csv", "application/json",
            "application/xml", "text/markdown",
        ) or filename.lower().endswith((".txt", ".md", ".csv", ".json")):
            content = data.decode("utf-8", errors="replace")[:6000]
            label = "Текст"

        else:
            await status_msg.edit_text(
                f"📎 «{title}» — тип {mime or 'неизвестен'}, не знаю как читать."
            )
            return

        if not content or not content.strip():
            await status_msg.edit_text(f"📎 «{title}» — не смог извлечь текст.")
            return

        added = await asyncio.to_thread(
            _add_knowledge_item, f"telegram_{label}", item_id, title, content
        )
        if added:
            summary = await _summarize_content(content, title)
            if summary:
                await status_msg.edit_text(
                    f"✅ {label} «{title}» добавлен в базу знаний.\n\n"
                    f"📋 Саммари:\n{summary}"
                )
            else:
                snippet = content[:200].replace("\n", " ")
                await status_msg.edit_text(
                    f"✅ {label} «{title}» добавлен в базу знаний.\n"
                    f"_{snippet}{'…' if len(content) > 200 else ''}_"
                )
        else:
            await status_msg.edit_text(f"📎 «{title}» уже в базе знаний.")

    except Exception as e:
        logger.error("on_file: %s", e)
        await status_msg.edit_text(f"⚠️ Не смог обработать «{title}»: {e}")


ANALYZE_SYSTEM_PROMPT = (
    "Ты анализируешь рабочую переписку небольшой команды (Лена и Глеб) по проекту "
    "Pastila OS. Найди КОНКРЕТНЫЕ задачи, поручения и явные пожелания/приоритеты — "
    "особенно то, что хочет или просит Глеб. Игнорируй болтовню и обсуждения без действия. "
    'Верни СТРОГО JSON: {"tasks": [ {title, who, deadline, dod, steps, tags, status}, ... ]}, '
    'максимум 5 самых конкретных. who — одно из «Лена»/«Глеб»/«Лена + Глеб»/""; '
    'deadline — ДД.ММ или ""; steps — массив строк; tags — массив слов без #; status всегда NEW. '
    'Если конкретных задач нет — верни {"tasks": []}.'
)


async def _gpt_analyze(transcript, model_id):
    """Просит модель найти задачи в переписке. Возвращает список dict."""
    now = datetime.datetime.now(TZINFO)
    kb = _kb_context(max_chars_per_item=400, max_total=3000)
    static_ctx = f"Сегодня {now:%d.%m.%Y}.\n\nПереписка:\n{transcript}"
    if kb:
        static_ctx += f"\n\nКонтекст:\n{kb}"
    content = await llm.call_llm(
        [llm.sys_cached(ANALYZE_SYSTEM_PROMPT),
         llm.user_with_cache(static_ctx, "Найди задачи.")],
        model_id, temperature=0, max_tokens=900,
        response_format={"type": "json_object"},
    )
    obj = llm.loads_loose(content)
    return obj.get("tasks", []) if isinstance(obj, dict) else []


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/analyze — разобрать накопленную переписку и предложить задачи."""
    if not llm.OPENROUTER_API_KEY:
        await update.message.reply_text("🔇 Анализ выключен — не задан OPENROUTER_API_KEY.")
        return
    buf = _CHAT_LOG.get(update.message.chat_id, [])
    if len(buf) < 3:
        await update.message.reply_text(
            "Пока мало сообщений для анализа. Я вижу переписку только с момента запуска — "
            "пообщайтесь в чате и вызови /analyze позже."
        )
        return
    await llm.choose_then_run(update, context, "analyze")


async def _run_analyze(update, context, pending):
    chat_id = update.effective_chat.id
    thread_id = update.effective_message.message_thread_id
    buf = _CHAT_LOG.get(chat_id, [])
    transcript = await llm.prepare_context(buf, chat_id, task="analyze")
    model_id, label, is_auto = llm.resolve_model(chat_id, "analyze", transcript)
    note = await context.bot.send_message(
        chat_id, f"🧠 Анализирую ({label}{' · авто' if is_auto else ''})…",
        message_thread_id=thread_id,
    )
    try:
        tasks = await _gpt_analyze(transcript, model_id)
    except Exception as e:
        logger.error("Анализ переписки: %s", e)
        await note.edit_text("⚠️ Не получилось проанализировать. Попробуй позже.")
        return
    if not tasks:
        await note.edit_text("Конкретных задач в переписке не нашёл 🤷")
        return
    await note.edit_text(
        f"Нашёл задач: {len(tasks)}. Ниже черновики — выбери статус, чтобы завести "
        "(или «Пропустить»):"
    )
    for parsed in tasks[:5]:
        data = _draft_from_parsed(parsed)
        draft = (
            f"— Черновик из переписки —\n{build_task_text(data)}\n\n"
            "🚦 Выбери статус — задача опубликуется:"
        )
        rows = [list(r) for r in status_keyboard("voicestatus").inline_keyboard]
        rows.append([InlineKeyboardButton("❌ Пропустить", callback_data="voice::cancel")])
        sent = await context.bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id, text=draft,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        context.chat_data.setdefault("voice_drafts", {})[sent.message_id] = data


def _chunks(text, size=4000):
    """Режет длинный текст на куски под лимит сообщения Telegram."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


PLAN_SYSTEM_PROMPT = (
    "Ты — планировщик команды Pastila OS (белёвская пастила).\n"
    "Глеб — контент, коммуникации, стратегия. Лена — разработка, техника, продукт.\n\n"
    "На основе переписки составь конкретный план работы — отдельно для каждого.\n"
    "Для каждого: что сделать, в каком порядке (по приоритету), срок если был.\n\n"
    "Формат ответа (строго):\n\n"
    "📋 Лена\n"
    "1. Задача — срок\n"
    "2. Задача — срок\n\n"
    "📋 Глеб\n"
    "1. Задача — срок\n"
    "2. Задача — срок\n\n"
    "Если по кому-то задач нет — напиши «Нет активных задач».\n"
    "Без вступлений, без markdown-звёздочек, без воды.\n\n"
    "Если видишь явное узкое место или риск — добавь в конце:\n"
    "⚠️ Обратите внимание: [конкретно и кратко]"
)


async def _gpt_plan(transcript, extra, model_id):
    """Просит модель составить план работы для Лены и Глеба. Возвращает текст."""
    now = datetime.datetime.now(TZINFO)
    kb = _kb_context(max_chars_per_item=500, max_total=4000)
    static_parts = [f"Сегодня {now:%d.%m.%Y}."]
    if extra:
        static_parts.append(f"Контекст: {extra}")
    static_parts.append(f"Переписка:\n{transcript}")
    if kb:
        static_parts.append(f"База знаний:\n{kb}")
    return await llm.call_llm(
        [llm.sys_cached(PLAN_SYSTEM_PROMPT),
         llm.user_with_cache("\n\n".join(static_parts), "Составь план.")],
        model_id, temperature=0.2, max_tokens=1500,
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/plan — план работы для Лены и Глеба по переписке (+ необязательный текст после команды)."""
    if not llm.OPENROUTER_API_KEY:
        await update.message.reply_text("🔇 Планировщик выключен — не задан OPENROUTER_API_KEY.")
        return
    extra = " ".join(context.args) if context.args else ""
    buf = _CHAT_LOG.get(update.message.chat_id, [])
    if len(buf) < 3 and not extra:
        await update.message.reply_text(
            "Маловато данных. Пообщайтесь и вызови /plan позже, либо добавь контекст "
            "после команды, например: /plan что нужно успеть к запуску."
        )
        return
    await llm.choose_then_run(update, context, "plan", pending={"extra": extra})


async def _run_plan(update, context, pending):
    chat_id = update.effective_chat.id
    extra = pending.get("extra", "")
    buf = _CHAT_LOG.get(chat_id, [])
    transcript = await llm.prepare_context(buf, chat_id, task="plan")
    model_id, label, is_auto = llm.resolve_model(chat_id, "plan", transcript + " " + extra)
    note = await context.bot.send_message(
        chat_id, f"🗂 Составляю план ({label}{' · авто' if is_auto else ''})…",
    )
    try:
        plan = await _gpt_plan(transcript, extra, model_id)
    except Exception as e:
        logger.error("План: %s", e)
        await note.edit_text("⚠️ Не получилось составить план. Попробуй позже.")
        return
    chunks = _chunks(plan.strip() or "Пусто.")
    await note.edit_text(chunks[0])
    for ch in chunks[1:]:
        await context.bot.send_message(chat_id, ch)


# ------------------------------------------------------------------
# СЕССИИ — /session: добавить текст / файл / голос в базу знаний
# ------------------------------------------------------------------
# Двухшаговый флоу без полноценного ConversationHandler:
#   Шаг 1: /session Название  → бот запоминает название в chat_data
#   Шаг 2: следующее сообщение (текст / файл / голос) → контент сессии → KB
# ------------------------------------------------------------------

_SESSION_KEY = "_session_pending"  # ключ в chat_data


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/session [название] — начать запись сессии/встречи/лога в базу знаний."""
    title = " ".join(context.args).strip() if context.args else ""
    now = datetime.datetime.now(TZINFO)
    date_str = now.strftime("%d.%m.%Y")
    if not title:
        title = f"Сессия {date_str}"
    context.chat_data[_SESSION_KEY] = title
    await update.message.reply_text(
        f"📝 Готова записать сессию «{title}».\n\n"
        "Пришли текст, вставь скопированный контент, отправь файл (PDF, DOCX, .txt) "
        "или голосовое — сохраню в базу знаний.\n\n"
        "/cancel — отменить."
    )


async def _handle_session_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Если в chat_data есть ожидающая сессия — обрабатывает текст. Возвращает True если обработал."""
    title = context.chat_data.get(_SESSION_KEY)
    if not title:
        return False
    msg = update.message
    if not msg or not msg.text or msg.text.startswith("/"):
        return False

    content = msg.text.strip()
    if len(content) < 10:
        await msg.reply_text("Слишком коротко. Пришли содержание сессии.")
        return True

    context.chat_data.pop(_SESSION_KEY, None)
    now = datetime.datetime.now(TZINFO)
    item_id = f"session_{hashlib.md5(f'{title}{now}'.encode()).hexdigest()[:12]}"
    full_title = f"Сессия: {title} ({now.strftime('%d.%m.%Y')})"

    added = await asyncio.to_thread(_add_knowledge_item, "session", item_id, full_title, content)
    if added:
        summary = await _summarize_content(content, title)
        if summary:
            await msg.reply_text(
                f"✅ Сессия «{title}» ({len(content)} симв.) добавлена в базу знаний.\n\n"
                f"📋 Саммари:\n{summary}"
            )
        else:
            snippet = content[:300].replace("\n", " ")
            await msg.reply_text(
                f"✅ Сессия «{title}» ({len(content)} симв.) добавлена в базу знаний.\n"
                f"_{snippet}…_"
            )
    else:
        await msg.reply_text(f"📎 Сессия «{title}» уже в базе знаний.")
    return True


async def _handle_session_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Если ждём сессию — обрабатывает файл/фото/голос как контент сессии."""
    title = context.chat_data.get(_SESSION_KEY)
    if not title:
        return False
    msg = update.message
    if not msg:
        return False

    # голосовое → Whisper
    if msg.voice:
        if not OPENAI_API_KEY:
            return False
        context.chat_data.pop(_SESSION_KEY, None)
        status = await msg.reply_text(f"🎙️ Транскрибирую сессию «{title}»…")
        try:
            data = await _download_tg_file(context.bot, msg.voice.file_id)
            content = await _whisper_transcribe(data)
        except Exception as e:
            await status.edit_text(f"⚠️ Не смог расшифровать: {e}")
            return True
        if not content:
            await status.edit_text("⚠️ Пустая расшифровка.")
            return True
        now = datetime.datetime.now(TZINFO)
        item_id = f"session_{hashlib.md5(f'{title}{now}'.encode()).hexdigest()[:12]}"
        full_title = f"Сессия (аудио): {title} ({now.strftime('%d.%m.%Y')})"
        await asyncio.to_thread(_add_knowledge_item, "session", item_id, full_title, content)
        await status.edit_text(
            f"✅ Аудио-сессия «{title}» расшифрована и добавлена в базу знаний.\n"
            f"_{content[:250].replace(chr(10), ' ')}…_"
        )
        return True

    # файл / фото → on_file уже умеет, но нам нужно переопределить title
    doc = msg.document
    photo = msg.photo[-1] if msg.photo else None
    if not doc and not photo:
        return False

    context.chat_data.pop(_SESSION_KEY, None)
    filename = (doc.file_name if doc else "photo.jpg") or "file"
    mime = (doc.mime_type if doc else "image/jpeg") or ""
    file_id = (doc.file_id if doc else photo.file_id)
    size = (doc.file_size if doc else photo.file_size) or 0

    if size > 20 * 1024 * 1024:
        await msg.reply_text("Файл > 20 МБ, Telegram не даёт скачать.")
        return True

    status = await msg.reply_text(f"📎 Читаю файл для сессии «{title}»…")
    try:
        data = await _download_tg_file(context.bot, file_id)
        content = ""
        if mime == "application/pdf" or filename.lower().endswith(".pdf"):
            content = await asyncio.to_thread(_extract_pdf, data)
        elif filename.lower().endswith(".docx"):
            content = await asyncio.to_thread(_extract_docx, data)
        elif mime.startswith("image/"):
            content = await _vision_describe(data, filename)
        elif filename.lower().endswith((".txt", ".md", ".csv")):
            content = data.decode("utf-8", errors="replace")[:6000]
        else:
            content = ""

        if not content:
            await status.edit_text(f"⚠️ Не смог извлечь текст из «{filename}».")
            return True

        now = datetime.datetime.now(TZINFO)
        item_id = f"session_{hashlib.md5(f'{title}{filename}{now}'.encode()).hexdigest()[:12]}"
        full_title = f"Сессия: {title} [{filename}] ({now.strftime('%d.%m.%Y')})"
        await asyncio.to_thread(_add_knowledge_item, "session", item_id, full_title, content)
        snippet = content[:300].replace("\n", " ")
        await status.edit_text(
            f"✅ Файл для сессии «{title}» добавлен в базу знаний.\n"
            f"_{snippet}{'…' if len(content) > 300 else ''}_"
        )
    except Exception as e:
        logger.error("_handle_session_file: %s", e)
        await status.edit_text(f"⚠️ Ошибка: {e}")
    return True


# ------------------------------------------------------------------
# БАЗА ЗНАНИЙ — команды /notion и /kb
# ------------------------------------------------------------------

async def cmd_knowledge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kb — показать что сейчас в базе знаний."""
    if not _KNOWLEDGE:
        await update.message.reply_text(
            "База знаний пуста.\n"
            "• Закиньте файл (PDF, DOCX, фото, аудио, .txt) — прочитаю автоматически.\n"
            "• /session — добавить лог/сессию текстом или голосом.\n"
            "• /notion sync — подтянуть страницы из Notion.\n"
            "• /find [тема] — найти по ключевым словам или тегу."
        )
        return
    lines = [f"📚 База знаний — {len(_KNOWLEDGE)} элементов:\n"]
    for i, item in enumerate(_KNOWLEDGE, 1):
        src = item["source"].replace("telegram_", "").upper()
        tags = f"  {item['tags']}" if item.get("tags") else ""
        lines.append(f"{i}. [{src}] {item['title'][:60]} — {item['added_at'][:10]}{tags}")
    lines.append("\n/find [тема] — поиск. Голосом и в /plan, /analyze всё учитывается.")
    await update.message.reply_text("\n".join(lines))


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find [запрос/#тег] — поиск по базе знаний."""
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "Укажи что искать:\n"
            "/find стратегия\n"
            "/find #идея\n"
            "/find Анализ Semers\n\n"
            "Или голосом: «найди всё про монетизацию»"
        )
        return

    if not _KNOWLEDGE:
        await update.message.reply_text("База знаний пуста.")
        return

    results = _kb_search(query)
    if not results:
        await update.message.reply_text(f"По запросу «{query}» ничего не нашлось в базе знаний.")
        return

    lines = [f"🔍 По запросу «{query}» — {len(results)} результат(ов):\n"]
    for item in results:
        src = item["source"].replace("telegram_", "").upper()
        tags = f" {item['tags']}" if item.get("tags") else ""
        snippet = item["content"][:200].replace("\n", " ")
        lines.append(f"[{src}] {item['title'][:50]}{tags}\n_{snippet}…_\n")

    await update.message.reply_text("\n".join(lines)[:4000])


async def cmd_notion_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/notion sync — синхронизировать страницы из Notion в базу знаний."""
    args = context.args or []
    if not args or args[0].lower() != "sync":
        await update.message.reply_text(
            "Синхронизировать Notion → базу знаний:\n/notion sync\n\n"
            "Нужна переменная NOTION_TOKEN (Internal Integration Secret).\n"
            "Опционально: NOTION_PAGES — список page_id через запятую."
        )
        return

    if not NOTION_TOKEN:
        await update.message.reply_text("❌ Не задан NOTION_TOKEN.")
        return
    if not _NOTION_SDK_OK:
        await update.message.reply_text(
            "❌ Библиотека notion-client не установлена. Добавь в requirements.txt:\nnotion-client"
        )
        return

    status_msg = await update.message.reply_text("🔄 Подключаюсь к Notion…")

    try:
        client = NotionClient(auth=NOTION_TOKEN)

        # получаем список страниц для синка
        if NOTION_PAGES:
            page_ids = NOTION_PAGES
        else:
            # ищем все страницы доступные интеграции
            search_result = await client.search(filter={"property": "object", "value": "page"})
            page_ids = [r["id"] for r in search_result.get("results", [])][:30]

        if not page_ids:
            await status_msg.edit_text("Notion: нет доступных страниц. Поделись страницами с интеграцией.")
            return

        await status_msg.edit_text(f"🔄 Читаю {len(page_ids)} страниц из Notion…")

        added = 0
        skipped = 0
        for page_id in page_ids:
            try:
                page_meta = await client.pages.retrieve(page_id=page_id)
                # извлекаем заголовок
                props = page_meta.get("properties", {})
                title = ""
                for prop in props.values():
                    if prop.get("type") == "title":
                        rich = prop.get("title", [])
                        title = "".join(r.get("plain_text", "") for r in rich).strip()
                        break
                title = title or page_id[:8]

                content = await _notion_extract_blocks(client, page_id)
                if not content.strip():
                    skipped += 1
                    continue

                ok = await asyncio.to_thread(
                    _add_knowledge_item, "notion", page_id, title, content
                )
                if ok:
                    added += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error("Notion page %s: %s", page_id, e)
                skipped += 1

        await client.aclose()
        await status_msg.edit_text(
            f"✅ Notion синхронизирован.\n"
            f"Добавлено: {added}  ·  Пропущено/уже есть: {skipped}\n"
            f"Всего в базе знаний: {len(_KNOWLEDGE)}"
        )

    except Exception as e:
        logger.error("cmd_notion_sync: %s", e)
        await status_msg.edit_text(f"⚠️ Ошибка Notion: {e}")


async def _notion_extract_blocks(client, block_id: str, depth: int = 0) -> str:
    """Рекурсивно извлекает текст из блоков Notion-страницы."""
    if depth > 3:
        return ""
    try:
        resp = await client.blocks.children.list(block_id=block_id, page_size=100)
    except Exception:
        return ""
    lines = []
    for block in resp.get("results", []):
        btype = block.get("type", "")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich).strip()
        if text:
            prefix = "  " * depth
            if btype.startswith("heading"):
                prefix += "# "
            elif btype in ("bulleted_list_item", "to_do"):
                prefix += "• "
            elif btype == "numbered_list_item":
                prefix += "  "
            lines.append(prefix + text)
        if block.get("has_children") and len(lines) < 200:
            child_text = await _notion_extract_blocks(client, block["id"], depth + 1)
            if child_text:
                lines.append(child_text)
    return "\n".join(lines)[:6000]


def _flatten_text(t):
    """Текст сообщения в экспорте бывает строкой или списком кусков — склеиваем в строку."""
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        out = []
        for p in t:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                out.append(p.get("text", ""))
        return "".join(out)
    return ""


def _parse_export(data):
    """Парсит result.json экспорта Telegram → [(имя, текст), ...]."""
    msgs = data.get("messages", []) if isinstance(data, dict) else []
    out = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("type") != "message":
            continue  # пропускаем сервисные сообщения
        text = _flatten_text(m.get("text", "")).strip()
        if text:
            out.append((str(m.get("from") or "?"), text))
    return out


async def on_history_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прислали result.json (экспорт чата) — грузим историю в память для /plan, /analyze, поиска."""
    msg = update.message
    doc = msg.document
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        return
    note = await msg.reply_text("📥 Загружаю историю чата…")
    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        data = json.loads(bytes(raw).decode("utf-8", "ignore"))
        imported = _parse_export(data)
    except Exception as e:
        logger.error("Импорт истории: %s", e)
        await note.edit_text(
            "⚠️ Не смог прочитать файл. Нужен result.json из экспорта Telegram "
            "(Export chat history → формат Machine-readable JSON)."
        )
        return
    if not imported:
        await note.edit_text("В файле не нашёл сообщений. Проверь, что экспорт в формате JSON.")
        return
    existing = _CHAT_LOG.get(msg.chat_id, [])
    _CHAT_LOG[msg.chat_id] = (imported + existing)[-_LOG_MAX:]
    await note.edit_text(
        f"📚 Загрузил историю: {len(imported)} сообщений. Теперь /plan, /analyze и голосовой "
        "поиск учитывают её.\n(Хранится в памяти до перезапуска бота.)"
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/id — показать chat_id и id топика. Помогает собрать переменные при настройке."""
    chat = update.effective_chat
    thread_id = update.message.message_thread_id  # None вне топиков
    lines = ["🆔 Идентификаторы для настройки:", "", f"`GROUP_CHAT_ID` = `{chat.id}`"]
    if thread_id is not None:
        lines.append(f"id этого топика = `{thread_id}`")
        lines.append("")
        lines.append("→ впиши его в `THREAD_LENA` или `THREAD_GLEB` (смотря чей это топик).")
    else:
        lines.append("Это General или личка — id топика нет. Запусти /id внутри нужного топика.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_ai_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ai — проверка ключей: OpenAI (Whisper) и OpenRouter (LLM-функции)."""
    lines = []
    # 1) OpenAI → только Whisper (голосовое распознавание)
    if not OPENAI_API_KEY:
        lines.append("🔇 OPENAI_API_KEY не задан — голосовой ввод выключен.")
    else:
        note = await update.message.reply_text("🔌 Проверяю OpenAI (Whisper)…")
        payload = {
            "model": OPENAI_MODEL, "max_tokens": 5,
            "messages": [{"role": "user", "content": "ping"}],
        }
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions", headers=headers, json=payload
                )
            if r.status_code == 200:
                lines.append("✅ OpenAI на связи — голосовое распознавание (Whisper) работает.")
            else:
                try:
                    err = r.json().get("error", {}).get("message", "") or r.text[:200]
                except Exception:
                    err = r.text[:200]
                hints = {
                    401: "Ключ неверный. Проверь OPENAI_API_KEY.",
                    429: "Нет квоты. Зайди в Billing на platform.openai.com.",
                    404: f"Модель «{OPENAI_MODEL}» недоступна для ключа.",
                }
                hint = hints.get(r.status_code, "")
                lines.append(f"❌ OpenAI: HTTP {r.status_code}. {err}" + (f" {hint}" if hint else ""))
        except Exception as e:
            lines.append(f"⚠️ OpenAI недоступен (сеть): {e}")
        await note.delete()
    # 2) OpenRouter → /analyze, /plan, /model и разбор голоса
    if not llm.OPENROUTER_API_KEY:
        lines.append("🔇 OPENROUTER_API_KEY не задан — /analyze, /plan, /model и разбор голоса выключены.")
    else:
        lines.append(f"✅ OPENROUTER_API_KEY задан — LLM-функции работают через OpenRouter.")
    await update.message.reply_text("\n".join(lines) or "Всё настроено.")


# Приветствие-пояснение: показывается на /start и /help. HTML — для жирных заголовков.
START_TEXT = (
    "👋 <b>Привет! Я Pastila OS — бот для задач.</b>\n\n"
    "Помогаю вести задачи команды прямо в Telegram: чтобы поручения не терялись "
    "в переписке, было видно <b>кто что делает и к какому сроку</b>, а дедлайны "
    "не забывались.\n\n"
    "<b>🎯 Какие задачи решаю</b>\n"
    "• Поручения не теряются — каждая задача оформлена и записана в таблицу.\n"
    "• Видно, кто за что отвечает и какой статус у дела.\n"
    "• Сам напоминаю о дедлайнах — утром и за день до срока.\n"
    "• Не хочешь печатать — <b>наговори голосом</b>, я оформлю задачу.\n"
    "• Разберу переписку и предложу из неё задачи и план работы.\n\n"
    "<b>⚡ С чего начать</b>\n"
    "• <code>/new</code> — завести задачу по шагам\n"
    "• Или просто <b>пришли голосовое</b> с поручением\n"
    "• <code>/list</code> — посмотреть открытые задачи\n\n"
    "<b>📋 Команды</b>\n"
    "<code>/new</code> — создать задачу\n"
    "<code>/list</code> — открытые задачи\n"
    "<code>/status</code> — сменить статус (ответом на задачу)\n"
    "<code>/digest</code> — дедлайны на сегодня\n"
    "<code>/alerts</code> — дедлайны на завтра\n"
    "<code>/analyze</code> — найти задачи в переписке\n"
    "<code>/plan</code> — план работы для Лены и Глеба\n"
    "<code>/id</code> — chat_id и id топика (для настройки)\n"
    "<code>/cancel</code> — отменить создание\n\n"
    "<b>🎙️ Голос</b>\n"
    "Наговори задачу — заведу. Или спроси «найди, где Глеб говорил про сроки» — "
    "поищу ответ в переписке.\n\n"
    "<b>📚 История</b>\n"
    "Пришли файл <code>result.json</code> (экспорт чата) — учту всю прошлую "
    "переписку в <code>/plan</code> и <code>/analyze</code>."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, parse_mode="HTML")


# Приветствие при добавлении бота в группу: баннер-пастила + короткое описание.
WELCOME_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "welcome.png")
WELCOME_CAPTION = (
    "👋 <b>Привет! Я Pastila OS — бот для задач команды.</b>\n"
    "Чтобы поручения не терялись: каждая задача оформлена и записана — видно "
    "<b>кто, что и к какому сроку</b>.\n\n"
    "<b>📋 Команды</b>\n"
    "<code>/new</code> — создать задачу (по шагам)\n"
    "<code>/menu</code> — меню действий (приоритет · план · статусы)\n"
    "<code>/list</code> — открытые задачи\n"
    "<code>/status</code> — сменить статус (ответом на задачу)\n"
    "<code>/digest</code> — дедлайны на сегодня\n"
    "<code>/alerts</code> — дедлайны на завтра\n"
    "<code>/analyze</code> — найти задачи в переписке\n"
    "<code>/plan</code> — план для Лены и Глеба\n"
    "<code>/id</code> — ID этого чата\n"
    "<code>/help</code> — что умеет бот\n\n"
    "🎙️ Или просто <b>пришлите голосовое</b> — оформлю задачу.\n\n"
    "👇 Нажмите «Подробнее» — расскажу, как всё работает."
)

# «Подробнее» = меню разделов (кнопки). Текст каждого раздела — простым языком.
HELP_MENU_TEXT = (
    "<b>📖 Как всё работает</b>\n\n"
    "Выберите раздел — расскажу подробно и понятно.\n"
    "Кнопками можно листать туда и обратно. 🍬"
)

HELP_SECTIONS = {
    "new": (
        "📝 Завести задачу",
        "<b>📝 Завести задачу — три способа</b>\n\n"
        "1. <code>/new</code> — бот по шагам спросит: название, критерий готовности (DoD), "
        "кто (Лена / Глеб / оба), дедлайн, шаги, материалы (можно приложить файл или фото), "
        "теги и статус. Любой шаг можно пропустить.\n\n"
        "2. <b>Голосом</b> — просто наговорите поручение. Бот распознает речь и сам соберёт "
        "черновик задачи — останется выбрать статус, и она опубликуется.\n\n"
        "3. <b>Из переписки</b> — <code>/analyze</code> прочитает обсуждение в чате и "
        "предложит готовые задачи. Подтверждаете нужные — они заводятся.",
    ),
    "task": (
        "📌 Что с задачей",
        "<b>📌 Что происходит с задачей</b>\n\n"
        "• В группе появляется аккуратная <b>карточка</b>: кто отвечает, срок, шаги, теги, "
        "статус.\n"
        "• Прямо под карточкой — <b>кнопки статуса в один тап</b>: В работу · Ждём · Ревью · "
        "Блок · Done. Нажатие меняет статус и в карточке, и в таблице.\n"
        "• Каждая задача автоматически попадает строкой в <b>Google-таблицу</b> "
        "(дата, кто, задача, дедлайн, статус, ссылка) — это общий реестр.\n"
        "• <code>/list</code> покажет все открытые задачи, сгруппированные по людям.",
    ),
    "remind": (
        "⏰ Напоминания",
        "<b>⏰ Напоминания — бот следит за сроками сам</b>\n\n"
        "• <b>Утром</b> присылает задачи, у которых дедлайн сегодня.\n"
        "• <b>За день до срока</b> — отдельный алерт с тегом ответственного, чтобы не "
        "забыли.\n\n"
        "Проверить вручную можно в любой момент:\n"
        "<code>/digest</code> — дедлайны на сегодня\n"
        "<code>/alerts</code> — дедлайны на завтра",
    ),
    "voice": (
        "🎙️ Голос",
        "<b>🎙️ Голосовые — два режима</b>\n\n"
        "Просто отправьте боту голосовое:\n\n"
        "• <b>Поручение</b> («Лене сверстать лендинг к 25 июля») → бот оформит задачу.\n"
        "• <b>Вопрос по переписке</b> («найди, где договаривались про оплату») → бот "
        "поищет ответ в истории чата и процитирует, кто что говорил.\n\n"
        "Бот сам понимает, поручение это или вопрос.",
    ),
    "plan": (
        "🗂 Планы и история",
        "<b>🗂 Планы и память о прошлом</b>\n\n"
        "• <code>/plan</code> — бот соберёт из переписки конкретный план работы, отдельно "
        "для Лены и отдельно для Глеба (по приоритету и срокам).\n\n"
        "• <b>Импорт истории</b> — пришлите боту файл экспорта чата <code>result.json</code> "
        "(Telegram → Export chat history → JSON). Бот учтёт всю прошлую переписку в поиске, "
        "<code>/analyze</code> и <code>/plan</code>.",
    ),
    "integr": (
        "🔌 Интеграции",
        "<b>🔌 Интеграции</b>\n\n"
        "• <b>Telegram</b> — задачи живут прямо в вашей группе; бот раскладывает их по темам "
        "или помечает тегом ответственного.\n"
        "• <b>Google Sheets</b> — автоматический реестр всех задач и статусов в одной "
        "таблице.\n"
        "• <b>ИИ (распознавание речи + анализ текста)</b> — отвечает за голосовой ввод, "
        "поиск по переписке, авто-задачи из обсуждений и планы.\n\n"
        "Коротко: вы пишете или говорите — бот оформляет, складывает в таблицу, напоминает "
        "о сроках и помогает планировать. 🍬",
    ),
}


def help_menu_keyboard():
    """Кнопки разделов справки — по 2 в ряд."""
    keys = list(HELP_SECTIONS)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([
            InlineKeyboardButton(HELP_SECTIONS[k][0], callback_data=f"help::{k}")
            for k in keys[i:i + 2]
        ])
    return InlineKeyboardMarkup(rows)


def help_back_keyboard():
    """Кнопка возврата к списку разделов."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ К разделам", callback_data="help::menu")]]
    )


def _welcome_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧭 Открыть меню", callback_data="menu::open")],
        [InlineKeyboardButton("📖 Подробнее — как всё работает", callback_data="welcomeinfo::how")],
    ])


async def _send_welcome(bot, chat_id, thread_id=None):
    """Шлёт приветственный баннер с описанием команд и кнопкой «Подробнее».
    Если картинки нет — отправляет только текст (тоже с кнопкой)."""
    try:
        with open(WELCOME_IMAGE, "rb") as img:
            await bot.send_photo(
                chat_id=chat_id, message_thread_id=thread_id,
                photo=img, caption=WELCOME_CAPTION, parse_mode="HTML",
                reply_markup=_welcome_keyboard(),
            )
    except FileNotFoundError:
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=WELCOME_CAPTION, parse_mode="HTML",
            reply_markup=_welcome_keyboard(),
        )


async def on_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бота добавили в группу — здороваемся баннером с описанием."""
    cmu = update.my_chat_member
    if cmu is None:
        return
    was_out = cmu.old_chat_member.status in (ChatMember.LEFT, ChatMember.BANNED)
    now_in = cmu.new_chat_member.status in (
        ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER
    )
    if was_out and now_in and cmu.chat.type in ("group", "supergroup"):
        logger.info("Добавлен в группу %s (%s) — шлю приветствие", cmu.chat.id, cmu.chat.title)
        try:
            await _send_welcome(context.bot, cmu.chat.id)
        except Exception as e:
            logger.error("Не смог отправить приветствие: %s", e)


async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/welcome — показать приветственный баннер (проверить, не переподключая бота)."""
    await _send_welcome(context.bot, update.effective_chat.id, update.message.message_thread_id)


async def on_welcome_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Подробнее» под приветствием — открываем меню разделов отдельным сообщением."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        HELP_MENU_TEXT, parse_mode="HTML", reply_markup=help_menu_keyboard()
    )


async def on_help_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Навигация по справке: меню разделов ⇄ текст раздела (редактируем то же сообщение)."""
    query = update.callback_query
    await query.answer()
    key = query.data.split("::", 1)[1]
    if key == "menu":
        await query.edit_message_text(
            HELP_MENU_TEXT, parse_mode="HTML", reply_markup=help_menu_keyboard()
        )
        return
    section = HELP_SECTIONS.get(key)
    if not section:
        return
    await query.edit_message_text(
        section[1], parse_mode="HTML", reply_markup=help_back_keyboard()
    )


# ------------------------------------------------------------------
# ИНТЕРАКТИВНОЕ ОПИСАНИЕ ГРУППЫ (/pin)
# ------------------------------------------------------------------
_SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
    if SHEET_ID else "https://docs.google.com/spreadsheets"
)

PIN_MAIN_TEXT = (
    "🍬 <b>Pastila OS — рабочее пространство команды</b>\n\n"
    "Здесь живёт всё: задачи, решения, база знаний и ИИ-помощник.\n"
    "Два бота, одна таблица — всё под рукой.\n\n"
    "👥 <b>Глеб</b> @foxruso — контент, коммуникации, стратегия, шеф\n"
    "👩‍💻 <b>Лена</b> @elenaisanewleet — разработка, техника, продукт\n\n"
    "🤖 @PastilaTaskBot — задачи, планирование, база знаний, ИИ-анализ\n"
    "💻 @pastila_code_remote_bot — Claude Code, код, архитектура, деплой\n\n"
    f'📊 <a href="{_SHEET_URL}">Google Sheets — таблица задач</a>\n\n'
    "Выберите раздел ↓"
)

PIN_SECTIONS = {
    "tasks": (
        "📋 Задачи",
        "<b>📋 Как работают задачи</b>\n\n"
        "<b>3 способа создать задачу:</b>\n\n"
        "1️⃣ <code>/new</code> — диалог по шагам:\n"
        "   название → критерий готовности (DoD) → кто (Лена / Глеб / оба) → дедлайн → "
        "шаги → материалы → теги → статус\n"
        "   Любой шаг можно пропустить командой /skip\n\n"
        "2️⃣ <b>Голосом</b> — запишите поручение, бот сам оформит задачу\n\n"
        "3️⃣ <code>/analyze</code> — бот прочитает переписку и предложит задачи из обсуждения\n\n"
        "<b>Что происходит после создания:</b>\n"
        "• Карточка задачи публикуется в чат\n"
        "• Под карточкой — кнопки статуса в один тап\n"
        "• Строка автоматически добавляется в Google Sheets\n"
        "• При смене статуса — таблица обновляется\n\n"
        "<b>Статусы:</b>\n"
        "⚪️ NEW · 🟡 TODO · 🔵 WIP · 🟠 WAITING · 🟣 REVIEW · 🟢 DONE · 🔴 BLOCKED · ⚫️ CANCELLED\n\n"
        "<b>Полезные команды:</b>\n"
        "<code>/list</code> — открытые задачи по людям\n"
        "<code>/status</code> — сменить статус (ответом на карточку задачи)\n"
        "<code>/purge</code> — очистить всю таблицу (с подтверждением)",
    ),
    "voice": (
        "🎙 Голос",
        "<b>🎙 Голосовое управление</b>\n\n"
        "Просто запишите голосовое — бот поймёт, что вы хотите:\n\n"
        "<b>📌 Создать задачу:</b>\n"
        "<i>«поставь задачу Лене — сделать лендинг к 25 июля»</i>\n"
        "<i>«напомни Глебу про ответ клиенту»</i>\n\n"
        "<b>📋 Список и статусы:</b>\n"
        "<i>«покажи задачи Глеба»</i>\n"
        "<i>«что сейчас в работе»</i>\n\n"
        "<b>🗂 Планирование:</b>\n"
        "<i>«составь план на неделю»</i>\n"
        "<i>«что самое важное сейчас»</i> → приоритеты с обоснованием\n\n"
        "<b>🔍 Поиск по истории:</b>\n"
        "<i>«найди, где договаривались про оплату»</i>\n"
        "<i>«кто говорил про поставщика»</i>\n\n"
        "<b>🤔 Анализ и советы:</b>\n"
        "<i>«Глеб прав в этом решении?»</i> → честный анализ\n"
        "<i>«куда развивать бизнес»</i> → стратегический совет\n"
        "<i>«разбери этот конфликт»</i> → взвешенный взгляд\n\n"
        "Бот сам определяет намерение — задача, вопрос или план.",
    ),
    "kb": (
        "📚 База знаний",
        "<b>📚 База знаний — всё важное под рукой</b>\n\n"
        "Группа работает как умное хранилище: бот читает всё, что вы присылаете.\n\n"
        "<b>📎 Файлы (автоматически):</b>\n"
        "Просто кидайте в чат — бот сам прочитает и сохранит:\n"
        "• PDF — извлекает текст\n"
        "• DOCX — полное содержимое\n"
        "• Фото, схемы, скриншоты — описывает через vision-ИИ\n"
        "• Аудио — транскрибирует через Whisper\n"
        "После загрузки бот присылает краткое саммари (3–5 предложений)\n\n"
        "<b>💬 Сессии Claude / ChatGPT:</b>\n"
        "<code>/session Название</code> → вставьте текст диалога → бот сохранит и сделает саммари\n"
        "Или: экспортируйте страницу Claude в PDF и кидайте прямо в чат\n\n"
        "<b>🔗 Notion:</b>\n"
        "<code>/notion sync</code> — подтягивает страницы из Notion в базу\n\n"
        "<b>🔍 Поиск:</b>\n"
        "<code>/find стратегия</code> — поиск по ключевому слову\n"
        "<code>/find #идея</code> — поиск по тегу\n"
        "<code>/kb</code> — весь список базы знаний\n\n"
        "<b>⭐ Детектор важных мыслей:</b>\n"
        "Бот читает переписку и сам замечает ценные идеи →\n"
        "ставит ⭐ реакцию + сохраняет с тегами в базу знаний\n"
        "Теги: #идея #решение #стратегия #риск #клиенты #продукт #финансы #процесс",
    ),
    "ai": (
        "🤖 ИИ-анализ",
        "<b>🤖 ИИ-анализ и планирование</b>\n\n"
        "<b>Команды анализа:</b>\n"
        "<code>/analyze</code> — находит задачи в переписке, предлагает создать их\n"
        "<code>/plan</code> — конкретный план для Лены и Глеба по приоритетам и срокам\n"
        "<code>/menu</code> — быстрое меню: приоритеты · план · статусы\n\n"
        "<b>База знаний в контексте:</b>\n"
        "Бот автоматически включает базу знаний в запросы к ИИ —\n"
        "так ответы учитывают все загруженные материалы и прошлые решения\n\n"
        "<b>30+ моделей на выбор:</b>\n"
        "<code>/model</code> — сменить модель для текущего типа задачи\n"
        "<code>/trim</code> — настроить уровень сжатия контекста (1–5)\n\n"
        "<b>Доступные модели (OpenRouter):</b>\n"
        "• Быстрые/дешёвые: Haiku 4.5, Gemini Flash\n"
        "• Умные: Sonnet 4.5, GPT-4o, Gemini Pro\n"
        "• Топ: Opus 4, GPT-4.5, Gemini Ultra\n\n"
        "<b>Авто-режим:</b> бот сам выбирает модель по сложности запроса\n\n"
        "<code>/ai</code> — проверить соединение с OpenAI / OpenRouter",
    ),
    "alerts": (
        "⏰ Дедлайны",
        "<b>⏰ Дедлайны и напоминания</b>\n\n"
        "Бот следит за сроками сам — вам не нужно помнить:\n\n"
        "<b>Автоматически каждый день в 12:00 МСК:</b>\n"
        "• <code>/digest</code> — дедлайны на сегодня (кому и что)\n"
        "• <code>/alerts</code> — предупреждение: что горит завтра\n\n"
        "<b>Проверить вручную в любой момент:</b>\n"
        "<code>/digest</code> — посмотреть на сегодня\n"
        "<code>/alerts</code> — посмотреть на завтра\n\n"
        "<b>Формат карточки дедлайна:</b>\n"
        "Кому → Задача → Срок\n"
        "Если задача просрочена — бот это тоже покажет\n\n"
        "<b>Настройка времени (в render.yaml):</b>\n"
        "DIGEST_HOUR=12 · DIGEST_MINUTE=0\n"
        "ALERT_HOUR=12 · ALERT_MINUTE=0\n"
        "TZ=Europe/Moscow",
    ),
    "commands": (
        "⚙️ Все команды",
        "<b>⚙️ Полный список команд — @PastilaTaskBot</b>\n\n"
        "<b>Задачи:</b>\n"
        "<code>/new</code> — создать задачу (диалог по шагам)\n"
        "<code>/list</code> — открытые задачи по людям\n"
        "<code>/status</code> — сменить статус (ответом на карточку)\n"
        "<code>/digest</code> — дедлайны на сегодня\n"
        "<code>/alerts</code> — дедлайны на завтра\n"
        "<code>/recurring</code> — повторяющиеся задачи\n"
        "<code>/purge</code> — очистить все задачи из таблицы\n\n"
        "<b>Быстрые действия:</b>\n"
        "<code>/q [идея]</code> — мгновенно сохранить мысль в KB\n"
        "<code>/dash</code> — сводка: задачи, просрочены, KB\n"
        "<code>/post [идея]</code> — черновик поста для соцсетей\n"
        "<code>/remind_when слово — напоминание</code> — триггер по слову в чате\n\n"
        "<b>ИИ-анализ:</b>\n"
        "<code>/analyze</code> — найти задачи в переписке\n"
        "<code>/plan</code> — план для Лены и Глеба\n"
        "<code>/strategy</code> — стратегический совет (Opus + thinking)\n"
        "<code>/deep</code> — глубокий анализ всего: задачи + KB + проблемы + план\n"
        "<code>/menu</code> — быстрое интерактивное меню\n\n"
        "<b>База знаний:</b>\n"
        "<code>/session [название]</code> — добавить лог из Claude/GPT\n"
        "<code>/find [слово/#тег]</code> — поиск по базе\n"
        "<code>/kb</code> — весь список базы знаний\n"
        "<code>/notion sync</code> — синхронизация с Notion\n\n"
        "<b>Настройки ИИ:</b>\n"
        "<code>/model</code> — выбрать языковую модель (30+)\n"
        "<code>/trim</code> — уровень сжатия контекста\n"
        "<code>/ai</code> — проверить соединение\n\n"
        "<b>Управление:</b>\n"
        "<code>/pin</code> — показать этот гайд\n"
        "<code>/welcome</code> — баннер бота\n"
        "<code>/cancel</code> — отменить диалог\n\n"
        "<b>@pastila_code_remote_bot</b> — Claude Code:\n"
        "Написать любую задачу текстом → Claude напишет код, задеплоит.",
    ),
    "team": (
        "👥 Команда",
        "<b>👥 Кто мы и зачем всё это</b>\n\n"
        "<b>Лена</b> @elenaisanewleet\n"
        "Разработка, техника, продукт.\n"
        "Пишет код, строит инфраструктуру, запускает фичи.\n\n"
        "<b>Глеб</b> @foxruso\n"
        "Контент, коммуникации, стратегия, шеф.\n"
        "Клиенты, позиционирование, большая картинка.\n\n"
        "<b>Наш продукт:</b>\n"
        "🍬 Белёвская пастила — живой продукт с историей.\n"
        "Производство, продажи, маркетинг — всё в одних руках.\n\n"
        "<b>Зачем нам этот бот:</b>\n"
        "Задачи терялись в переписке → теперь у каждой есть карточка\n"
        "Решения забывались → теперь есть база знаний\n"
        "Планирование было хаосом → теперь ИИ помогает расставить приоритеты\n\n"
        f'📊 <a href="{_SHEET_URL}">Открыть таблицу задач</a>',
    ),
    "terminal": (
        "💻 Терминал и Claude",
        "<b>💻 Как программировать через терминал</b>\n\n"
        "Весь код бота пишет <b>Claude Code</b> — ИИ-помощник прямо в терминале.\n"
        "Лена открывает терминал, говорит что нужно — Claude пишет код, тестирует и пушит.\n\n"
        "─────────────────────────\n"
        "<b>Как запустить</b>\n\n"
        "1. Открыть Terminal (Cmd+Space → Terminal)\n"
        "2. Перейти в папку проекта:\n"
        "   <code>cd ~/pastila_bot</code>\n"
        "3. Запустить Claude Code с Telegram-коннектором:\n"
        "   <code>claude --channels plugin:telegram@claude-plugins-official</code>\n\n"
        "   (без --channels: Claude работает только в терминале)\n"
        "   (с --channels: Claude читает чат и может отвечать прямо в Telegram)\n\n"
        "4. Написать задачу — например:\n"
        "   <i>«добавь команду /stats которая показывает статистику»</i>\n\n"
        "Claude читает код, вносит правки, проверяет, пушит в GitHub.\n"
        "Render видит push → деплоит → бот обновлён через 2–3 мин.\n\n"
        "─────────────────────────\n"
        "<b>Активные коннекторы</b>\n\n"
        "🔌 <b>Telegram</b> — Claude видит сообщения группы, отвечает в чат\n"
        "📝 <b>Notion</b> — читает и редактирует страницы\n"
        "🔍 <b>Atlassian</b> — Jira / Confluence (когда понадобится)\n"
        "🌐 <b>WebSearch / WebFetch</b> — поиск в интернете и чтение сайтов\n\n"
        "─────────────────────────\n"
        "<b>Полный цикл изменения</b>\n\n"
        "Терминал → задача Claude → код написан → git push → Render деплоит → бот обновлён\n\n"
        "Код: github.com/smmsemmers/pastila_bot",
    ),
    "story": (
        "📖 Как это устроено",
        "<b>📖 Как мы построили это рабочее пространство</b>\n\n"
        "Всё началось с простой проблемы: задачи терялись в переписке, идеи забывались, "
        "а решения из долгих разговоров исчезали бесследно.\n\n"
        "Мы не стали покупать CRM и корпоративные инструменты. "
        "Вместо этого Лена написала бота, который живёт прямо здесь — в нашем рабочем чате.\n\n"
        "─────────────────────────\n"
        "<b>Что внутри</b>\n\n"
        "· Python 3.12 + библиотека python-telegram-bot\n"
        "· Работает на облачном сервере Render (всегда онлайн)\n"
        "· Google Sheets — общая таблица задач, всегда актуальная\n"
        "· 30+ языковых моделей через OpenRouter (Claude, GPT, Gemini, DeepSeek)\n"
        "· Whisper от OpenAI — распознавание голосовых\n"
        "· Notion — синхронизация материалов\n"
        "· Prompt caching — экономия токенов на повторных запросах\n\n"
        "─────────────────────────\n"
        "<b>Что это нам даёт</b>\n\n"
        "<b>Ни одна задача не теряется.</b> Голосом, текстом или из переписки — "
        "каждая поручение превращается в карточку с исполнителем и сроком.\n\n"
        "<b>Решения не испаряются.</b> Важные идеи бот замечает сам, ставит ⭐ "
        "и сохраняет в базу знаний с тегами. Документы, фото, схемы — всё читается автоматически.\n\n"
        "<b>Не нужно помнить о дедлайнах.</b> Бот сам напоминает накануне "
        "и сразу даёт кнопки: Готово / В работу / Перенести.\n\n"
        "<b>ИИ как партнёр, а не инструмент.</b> Спросить «что сейчас важнее всего» "
        "или «Глеб прав в этом?» — и получить честный ответ с обоснованием.\n\n"
        "<b>Стратегия на серьёзной модели.</b> /strategy подключает Claude Opus "
        "с расширенным мышлением — для вопросов уровня «куда двигаться дальше».\n\n"
        "<b>Контент без усилий.</b> /post — черновик поста в голосе бренда "
        "из одной фразы.\n\n"
        "<b>Всё в одном месте.</b> Задачи, база знаний, планирование, аналитика, "
        "контент — один чат вместо пяти разных приложений.\n\n"
        "─────────────────────────\n"
        "Код открыт: github.com/smmsemmers/pastila_bot",
    ),
}


def pin_main_keyboard():
    keys = list(PIN_SECTIONS)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([
            InlineKeyboardButton(PIN_SECTIONS[k][0], callback_data=f"pin::{k}")
            for k in keys[i:i + 2]
        ])
    return InlineKeyboardMarkup(rows)


def pin_back_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Главная", callback_data="pin::menu")]]
    )


# ------------------------------------------------------------------
# /deep — глубокий анализ: задачи + KB + переписка + проблемы + план
# ------------------------------------------------------------------
_DEEP_PROMPT = (
    "Ты — старший партнёр и бизнес-аналитик команды Pastila OS.\n"
    "Глеб (@foxruso) — контент, коммуникации, стратегия, шеф.\n"
    "Лена (@elenaisanewleet) — разработка, техника, продукт.\n"
    "Продукт: белёвская пастила ручной работы, Тульская область.\n\n"
    "Тебе переданы: все открытые задачи, база знаний, переписка команды.\n"
    "Твоя задача — провести ГЛУБОКИЙ анализ и выдать структурированный доклад.\n\n"
    "Структура доклада:\n\n"
    "📊 Текущая ситуация\n"
    "Что происходит в бизнесе прямо сейчас — честная картина без прикрас.\n\n"
    "🔴 Проблемы и узкие места\n"
    "Конкретные проблемы с доказательствами из задач и переписки.\n"
    "Для каждой: суть → почему критично → что будет если не решить.\n\n"
    "🟡 Риски на горизонте\n"
    "Что может пойти не так в ближайшие 1–3 месяца.\n\n"
    "🟢 Возможности\n"
    "Что сейчас недоиспользуется или можно захватить.\n\n"
    "🎯 Приоритетный план действий\n"
    "Топ-5 конкретных шагов — кто, что, зачем, срок.\n"
    "Отсортировать по влиянию на бизнес.\n\n"
    "💡 Неочевидный инсайт\n"
    "Одно наблюдение которое команда, возможно, не замечает.\n\n"
    "Стиль: прямо, конкретно, без воды. Факты и выводы — не общие слова.\n"
    "Цитируй переписку и задачи когда это подкрепляет тезис."
)


async def cmd_deep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deep — двухэтапный анализ: Perplexity ищет в интернете, Opus синтезирует с внутренними данными."""
    msg = update.effective_message
    wait = await msg.reply_text(
        "🔬 Запускаю глубокий анализ…\n"
        "Займёт 2–3 минуты: сначала веб-исследование, потом синтез."
    )
    try:
        # ── Этап 0: читаем внутренние данные ──
        try:
            groups = await asyncio.to_thread(read_open_tasks)
            task_lines = []
            for who, tasks in groups.items():
                for t in tasks:
                    task_lines.append(
                        f"[{t['status']}] {who}: {t['title']} · дедлайн {t['deadline']}"
                    )
            tasks_ctx = "\n".join(task_lines) if task_lines else "Открытых задач нет."
        except Exception:
            task_lines = []
            tasks_ctx = "(задачи недоступны)"

        kb_ctx = _kb_context(max_chars_per_item=600, max_total=4000)
        chat_log = _CHAT_LOG.get(msg.chat_id, [])
        log_text = await llm.prepare_context(chat_log, msg.chat_id, task="analyze")
        now = datetime.datetime.now(TZINFO)

        # ── Этап 1: Perplexity Deep Research — поиск в интернете ──
        await wait.edit_text(
            "🌐 Этап 1/2 — Perplexity исследует рынок и конкурентов…\n(60–90 сек)"
        )
        web_research = ""
        try:
            web_research = await llm.call_llm(
                [
                    {"role": "system", "content": (
                        "Ты аналитик рынка. Ответь на русском языке. "
                        "Используй веб-поиск для получения актуальной информации."
                    )},
                    {"role": "user", "content": (
                        "Исследуй рынок белёвской пастилы и натуральных сладостей ручной работы в России (2024–2025):\n"
                        "1. Ключевые конкуренты — кто продаёт, цены, позиционирование\n"
                        "2. Тренды: здоровое питание, ремесленные продукты, подарочные наборы\n"
                        "3. Каналы продаж: маркетплейсы, офлайн, соцсети — что работает лучше\n"
                        "4. Что аудитория ценит и ищет (отзывы, форумы, соцсети)\n"
                        "5. Незанятые возможности\n\n"
                        "Только конкретные факты, цифры, названия. Без воды."
                    )},
                ],
                model_id=llm.MODELS["sonar_deep"]["id"],
                max_tokens=3000,
                temperature=0.2,
                timeout=150,
            )
        except Exception as e:
            logger.warning("Perplexity deep research failed: %s", e)
            web_research = f"(веб-исследование недоступно: {e})"

        # ── Этап 2: Opus — синтез внешнего + внутреннего ──
        await wait.edit_text(
            "🧠 Этап 2/2 — Opus синтезирует всё в стратегический доклад…\n(60–90 сек)"
        )
        static_ctx = (
            f"Дата анализа: {now:%d.%m.%Y}\n\n"
            f"=== ОТКРЫТЫЕ ЗАДАЧИ ({len(task_lines)}) ===\n{tasks_ctx}\n\n"
            f"=== БАЗА ЗНАНИЙ ===\n{kb_ctx or 'Пусто.'}\n\n"
            f"=== ПЕРЕПИСКА КОМАНДЫ ===\n{log_text or 'Нет данных.'}\n\n"
            f"=== ВЕБ-ИССЛЕДОВАНИЕ РЫНКА ===\n{web_research}"
        )
        report = await llm.call_llm(
            [llm.sys_cached(_DEEP_PROMPT),
             llm.user_with_cache(static_ctx, "Проведи глубокий анализ с учётом данных о рынке. Выдай полный доклад.")],
            model_id=llm.MODELS["opus48"]["id"],
            max_tokens=5000,
            thinking_budget=12000,
            timeout=210,
        )

        chunks = _chunks(report.strip(), size=4000)
        await wait.edit_text(f"🔬 <b>Глубокий анализ</b>\n\n{chunks[0]}", parse_mode="HTML")
        for ch in chunks[1:]:
            await msg.reply_text(ch, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_deep: %s", e)
        await wait.edit_text(f"⚠️ Ошибка при анализе: {e}")


_STRATEGY_PROMPT = (
    "Ты — партнёр команды Pastila OS (белёвская пастила, Тульская область).\n"
    "Глеб (@foxruso) — контент, коммуникации, стратегия, шеф.\n"
    "Лена (@elenaisanewleet) — разработка, техника, продукт.\n"
    "Цель: вырасти в живой бренд с историей и устойчивыми продажами.\n\n"
    "Тебе дают базу знаний команды и историю переписки.\n"
    "Ответь конкретно и прямо — как партнёр, а не консультант.\n\n"
    "Структура ответа:\n\n"
    "🔍 Узкое место\n"
    "Где сейчас настоящий стоп? Честно, без смягчений.\n\n"
    "🎯 ТОП-3 на 2 недели\n"
    "Конкретные действия — кто делает, что именно, зачем.\n\n"
    "💡 Большая идея на 3 месяца\n"
    "Одна — самая значимая.\n\n"
    "🚫 Стоп-лист\n"
    "Что НЕ делать прямо сейчас — и почему.\n\n"
    "Без вводных фраз. Без «конечно» и «безусловно». Говори как человек с опытом в теме."
)


# ------------------------------------------------------------------
# /q — быстрая идея в одну строку
# ------------------------------------------------------------------
async def cmd_quick_idea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/q [текст] — мгновенно сохраняет идею в базу знаний без диалога."""
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Напиши идею: /q текст идеи")
        return
    item_id = f"q_{hashlib.md5(text.encode()).hexdigest()[:10]}"
    title = text[:80]
    await asyncio.to_thread(_add_knowledge_item, "quick", item_id, title, text, ["идея"])
    await update.message.reply_text(
        f"✅ Сохранено в базу знаний #идея\n\n«{title}{'…' if len(text) > 80 else ''}»"
    )


# ------------------------------------------------------------------
# /dash — живая сводка одним сообщением
# ------------------------------------------------------------------
async def cmd_dash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dash — сводка: задачи по людям, просрочены, KB, последняя активность."""
    msg = await update.message.reply_text("📊 Читаю данные…")
    try:
        groups = await asyncio.to_thread(read_open_tasks)
    except Exception as e:
        await msg.edit_text(f"⚠️ Не смог прочитать таблицу: {e}")
        return

    today = datetime.datetime.now(TZINFO).strftime("%d.%m")
    overdue: list[str] = []
    wip_count = 0
    total = 0
    by_person: dict[str, int] = {}
    all_tasks = []
    for who, tasks in groups.items():
        by_person[who] = len(tasks)
        total += len(tasks)
        for t in tasks:
            all_tasks.append(t)
            if "WIP" in t["status"].upper():
                wip_count += 1
            dl = t.get("deadline", "")
            if dl and dl != "Backlog":
                try:
                    d = datetime.datetime.strptime(dl + f".{datetime.datetime.now(TZINFO).year}", "%d.%m.%Y")
                    if d.date() < datetime.datetime.now(TZINFO).date():
                        overdue.append(t["title"])
                except ValueError:
                    pass

    lines = [f"📊 Дашборд  ·  {today}", ""]
    for who in ["Лена", "Глеб", "Лена + Глеб"]:
        n = by_person.get(who, 0)
        if n:
            lines.append(f"{'👩‍💻' if 'Лена' in who and 'Глеб' not in who else '👨‍💼' if who == 'Глеб' else '👥'}  {who}: {n} задач")
    lines.append(f"\n🔵 В работе: {wip_count}")
    if overdue:
        lines.append(f"🔴 Просрочено: {len(overdue)}")
        for t in overdue[:3]:
            lines.append(f"   · {t[:50]}")
    else:
        lines.append("✅ Просроченных нет")
    lines.append(f"\n📚 База знаний: {len(_KNOWLEDGE)} записей")
    lines.append(f"📋 Открытых задач: {total}")
    await msg.edit_text("\n".join(lines))


# ------------------------------------------------------------------
# /post — черновик поста для соцсетей
# ------------------------------------------------------------------
_POST_PROMPT = (
    "Ты — копирайтер бренда Pastila OS (белёвская пастила ручной работы, Тульская область).\n"
    "Голос бренда: живой, тёплый, с характером. Не рекламный. Не казённый.\n"
    "Мы рассказываем историю продукта, людей и процесса — не продаём в лоб.\n\n"
    "Напиши пост для Telegram/ВКонтакте на основе идеи пользователя.\n"
    "Длина: 3–6 абзацев. Заканчивай чем-то живым — вопросом, деталью или призывом.\n"
    "Без хэштегов. Без эмодзи через каждое слово. Можно 1–2 по делу.\n"
    "Только текст поста — без предисловий и пояснений."
)

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/post [идея] — генерирует черновик поста для соцсетей."""
    idea = " ".join(context.args).strip()
    if not idea and update.message.reply_to_message:
        idea = update.message.reply_to_message.text or ""
    if not idea:
        await update.message.reply_text(
            "Напиши идею: /post про то что мы делаем пастилу вручную\n"
            "Или ответь на любое сообщение командой /post"
        )
        return
    if not llm.OPENROUTER_API_KEY:
        await update.message.reply_text("⚠️ Не задан OPENROUTER_API_KEY.")
        return
    wait = await update.message.reply_text("✍️ Пишу черновик…")
    try:
        model_id = llm.model_id_for(update.effective_chat.id, "plan", idea)
        text = await llm.call_llm(
            [llm.sys_cached(_POST_PROMPT),
             {"role": "user", "content": f"Идея: {idea}"}],
            model_id, temperature=0.7, max_tokens=800,
        )
        await wait.edit_text(f"✍️ Черновик поста\n\n{text.strip()}")
    except Exception as e:
        logger.error("cmd_post: %s", e)
        await wait.edit_text(f"⚠️ Ошибка: {e}")


# ------------------------------------------------------------------
# Длинное голосовое → структура (транскрипт + действия + решения)
# ------------------------------------------------------------------
_VOICE_MEMO_PROMPT = (
    "Ты — ассистент команды Pastila OS (белёвская пастила, Лена + Глеб).\n"
    "Тебе дали транскрипт длинного голосового — брейнсторм, совещание или поток мыслей.\n\n"
    "Выдай структуру:\n\n"
    "📝 Суть (2–3 предложения — о чём речь)\n\n"
    "✅ Действия\n"
    "· [кто] — [что] — [срок если был]\n\n"
    "💡 Ключевые решения или идеи\n"
    "· ...\n\n"
    "⚠️ Открытые вопросы (если есть)\n"
    "· ...\n\n"
    "Без лишних слов. Только то что реально было в тексте."
)

async def _handle_long_voice(msg, transcript: str, status_msg):
    """Длинное голосовое (>30 слов) → структурированный разбор."""
    model_id = llm.model_id_for(msg.chat_id, "plan", transcript)
    await status_msg.edit_text("🎙 Длинное голосовое — разбираю структуру…")
    try:
        result = await llm.call_llm(
            [llm.sys_cached(_VOICE_MEMO_PROMPT),
             {"role": "user", "content": f"Транскрипт:\n{transcript}"}],
            model_id, temperature=0.2, max_tokens=1000,
        )
        # предлагаем сохранить в KB
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Сохранить в базу знаний", callback_data=f"savememo::0"),
        ]])
        sent = await status_msg.edit_text(result.strip(), reply_markup=keyboard)
        # сохраняем транскрипт во временное хранилище по message_id
        if not hasattr(msg, "_memo_store"):
            pass
        _MEMO_STORE[sent.message_id] = {"title": transcript[:60], "content": transcript, "summary": result.strip()}
    except Exception as e:
        logger.error("long voice memo: %s", e)
        await status_msg.edit_text(f"⚠️ Ошибка разбора: {e}")

_MEMO_STORE: dict[int, dict] = {}

async def on_save_memo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Сохранить в KB» под разбором длинного голосового."""
    query = update.callback_query
    await query.answer()
    data = _MEMO_STORE.get(query.message.message_id)
    if not data:
        await query.edit_message_text(query.message.text + "\n\n⚠️ Данные не найдены.")
        return
    item_id = f"memo_{hashlib.md5(data['content'][:100].encode()).hexdigest()[:10]}"
    full = f"Транскрипт:\n{data['content']}\n\nРазбор:\n{data['summary']}"
    await asyncio.to_thread(_add_knowledge_item, "voice_memo", item_id, data["title"], full, ["голосовое", "встреча"])
    await query.edit_message_text(query.message.text + "\n\n✅ Сохранено в базу знаний.")


# ------------------------------------------------------------------
# Еженедельный отчёт — пятница 17:00
# ------------------------------------------------------------------
async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт: что закрыто, что открыто, что просрочено."""
    if not GROUP_CHAT_ID:
        return
    try:
        ws = get_worksheet()
        records = await asyncio.to_thread(ws.get_all_records)
    except Exception as e:
        logger.error("weekly_report: %s", e)
        return

    done, open_tasks, overdue = [], [], []
    today = datetime.datetime.now(TZINFO)
    week_ago = (today - datetime.timedelta(days=7)).strftime("%d.%m")

    for row in records:
        status = str(row.get("Статус", "")).strip()
        title = str(row.get("Задача", "")).strip()
        who = str(row.get("Кто", "")).strip()
        dl = str(row.get("Дедлайн", "")).strip()
        is_closed = any(m in status.upper() for m in CLOSED_MARKERS)
        if "DONE" in status.upper():
            done.append(f"{who}: {title}")
        elif not is_closed:
            open_tasks.append({"title": title, "who": who, "deadline": dl, "status": status})
            if dl and dl != "Backlog":
                try:
                    d = datetime.datetime.strptime(dl + f".{today.year}", "%d.%m.%Y")
                    if d.date() < today.date():
                        overdue.append(f"{who}: {title}")
                except ValueError:
                    pass

    lines = [f"📊 Итоги недели  ·  {today.strftime('%d.%m.%Y')}", ""]
    if done:
        lines.append(f"✅ Закрыто на этой неделе: {len(done)}")
        for t in done[-5:]:
            lines.append(f"   · {t[:60]}")
    else:
        lines.append("⚠️ На этой неделе ничего не закрыто.")
    lines.append("")
    lines.append(f"📋 Открытых задач: {len(open_tasks)}")
    if overdue:
        lines.append(f"🔴 Просрочено: {len(overdue)}")
        for t in overdue[:3]:
            lines.append(f"   · {t[:60]}")
    lines.append("")
    lines.append("Хорошей недели! 🍬")

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text="\n".join(lines),
    )


# ------------------------------------------------------------------
# /recurring — повторяющиеся задачи
# ------------------------------------------------------------------
_REC_TAB = "_recurring"
_REC_HEADERS = ["title", "who", "interval_days", "next_date", "tags"]


def _recurring_ws():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json or not SHEET_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sh = gspread.authorize(creds).open_by_key(SHEET_ID)
        try:
            return sh.worksheet(_REC_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(_REC_TAB, rows=200, cols=len(_REC_HEADERS))
            ws.append_row(_REC_HEADERS)
            return ws
    except Exception as e:
        logger.error("_recurring_ws: %s", e)
        return None


async def cmd_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/recurring [задача] — [кто] — каждые [N] дней  |  /recurring list  |  /recurring del [N]"""
    args = " ".join(context.args).strip()
    if not args or args == "list":
        ws = await asyncio.to_thread(_recurring_ws)
        if ws is None:
            await update.message.reply_text("⚠️ Не смог подключиться к таблице.")
            return
        records = await asyncio.to_thread(ws.get_all_records)
        if not records:
            await update.message.reply_text(
                "Повторяющихся задач нет.\n\n"
                "Добавить: /recurring Обновить прайс — Лена — каждые 14 дней"
            )
            return
        lines = ["🔁 Повторяющиеся задачи", ""]
        for i, r in enumerate(records, 1):
            lines.append(f"{i}. {r['title']} · {r['who']} · каждые {r['interval_days']} дн. · след. {r['next_date']}")
        lines.append("\nУдалить: /recurring del [номер]")
        await update.message.reply_text("\n".join(lines))
        return

    if args.startswith("del "):
        idx = int(args[4:].strip()) - 1
        ws = await asyncio.to_thread(_recurring_ws)
        records = await asyncio.to_thread(ws.get_all_records)
        if idx < 0 or idx >= len(records):
            await update.message.reply_text("⚠️ Неверный номер.")
            return
        await asyncio.to_thread(ws.delete_rows, idx + 2)
        await update.message.reply_text(f"✅ Удалено: {records[idx]['title']}")
        return

    # парсим "задача — кто — каждые N дней"
    import re as _re
    m = _re.search(r"(.+?)\s*—\s*(.+?)\s*—\s*каждые?\s*(\d+)\s*дн", args, _re.I)
    if not m:
        await update.message.reply_text(
            "Формат: /recurring Обновить прайс — Лена — каждые 14 дней\n"
            "Список: /recurring list\n"
            "Удалить: /recurring del 2"
        )
        return
    title, who, days = m.group(1).strip(), m.group(2).strip(), int(m.group(3))
    next_date = (datetime.datetime.now(TZINFO) + datetime.timedelta(days=days)).strftime("%d.%m.%Y")
    ws = await asyncio.to_thread(_recurring_ws)
    if ws is None:
        await update.message.reply_text("⚠️ Не смог подключиться к таблице.")
        return
    await asyncio.to_thread(ws.append_row, [title, who, days, next_date, ""])
    await update.message.reply_text(
        f"🔁 Добавлено\n\n{title}\n{who}  ·  каждые {days} дн.  ·  первый раз {next_date}"
    )


async def _check_recurring(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная проверка повторяющихся задач — создаёт задачи при наступлении даты."""
    ws = await asyncio.to_thread(_recurring_ws)
    if ws is None:
        return
    records = await asyncio.to_thread(ws.get_all_records)
    today = datetime.datetime.now(TZINFO)
    today_str = today.strftime("%d.%m.%Y")
    for idx, r in enumerate(records):
        try:
            next_d = datetime.datetime.strptime(r["next_date"], "%d.%m.%Y")
        except ValueError:
            continue
        if next_d.date() <= today.date():
            # создаём задачу в таблице
            date_str = today.strftime("%d.%m.%Y")
            interval = int(r.get("interval_days", 7))
            new_next = (today + datetime.timedelta(days=interval)).strftime("%d.%m.%Y")
            await asyncio.to_thread(
                append_task_to_sheet, date_str, r["who"], r["title"],
                new_next, "⚪️ NEW", "",
            )
            # обновляем next_date в recurring
            await asyncio.to_thread(ws.update_cell, idx + 2, 4, new_next)
            if GROUP_CHAT_ID:
                await context.bot.send_message(
                    GROUP_CHAT_ID,
                    f"🔁 Повторяющаяся задача создана\n\n{r['title']}\n{r['who']}  ·  след. {new_next}",
                )


# ------------------------------------------------------------------
# Триггерное напоминание (/remind_when)
# ------------------------------------------------------------------
_TRIGGERS: dict[int, list[dict]] = {}  # chat_id → [{keyword, reminder, author_id}]


async def cmd_remind_when(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remind_when [ключевое слово] — [что напомнить]"""
    args = " ".join(context.args).strip()
    import re as _re
    m = _re.match(r"(.+?)\s*—\s*(.+)", args)
    if not m:
        # показать активные триггеры
        chat_id = update.effective_chat.id
        triggers = _TRIGGERS.get(chat_id, [])
        if not triggers:
            await update.message.reply_text(
                "Активных триггеров нет.\n\n"
                "Добавить: /remind_when поставка — уточнить цену у поставщика"
            )
        else:
            lines = ["🔔 Активные триггеры", ""]
            for i, t in enumerate(triggers, 1):
                lines.append(f"{i}. Когда «{t['keyword']}» → {t['reminder']}")
            await update.message.reply_text("\n".join(lines))
        return
    keyword, reminder = m.group(1).strip().lower(), m.group(2).strip()
    chat_id = update.effective_chat.id
    _TRIGGERS.setdefault(chat_id, []).append({
        "keyword": keyword,
        "reminder": reminder,
        "author_id": update.effective_user.id,
    })
    await update.message.reply_text(
        f"🔔 Триггер установлен\n\n"
        f"Когда в чате появится «{keyword}» — напомню:\n{reminder}"
    )


async def _check_triggers(chat_id: int, text: str, bot) -> None:
    """Вызывается из on_log_message — проверяет триггеры."""
    triggers = _TRIGGERS.get(chat_id, [])
    if not triggers:
        return
    text_lower = text.lower()
    fired = []
    remaining = []
    for t in triggers:
        if t["keyword"] in text_lower:
            fired.append(t)
        else:
            remaining.append(t)
    _TRIGGERS[chat_id] = remaining
    for t in fired:
        try:
            await bot.send_message(
                chat_id,
                f"🔔 Триггер сработал!\n\nВ чате упомянули «{t['keyword']}»\n\n→ {t['reminder']}"
            )
        except Exception as e:
            logger.error("trigger notify: %s", e)


async def cmd_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strategy — стратегический анализ «что делать дальше» на самой мощной модели с thinking."""
    msg = update.effective_message
    wait = await msg.reply_text(
        "🧠 Думаю… Claude Opus 4.8 + extended thinking.\n"
        "Обычно занимает 30–60 сек."
    )
    try:
        kb_ctx = _kb_context(max_chars_per_item=1000, max_total=6000)
        chat_ctx = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in _CHAT_LOG.get(msg.chat_id, [])[-30:]
            if m.get("content")
        )
        user_content = ""
        if kb_ctx:
            user_content += f"=== База знаний ===\n{kb_ctx}\n\n"
        if chat_ctx:
            user_content += f"=== Последняя переписка ===\n{chat_ctx}\n\n"
        user_content += "Вопрос: что делать дальше, чтобы мы продвинулись и сделали крутой бизнес?"

        flagship_id = llm.MODELS["opus48"]["id"]
        answer = await llm.call_llm(
            [llm.sys_cached(_STRATEGY_PROMPT),
             {"role": "user", "content": user_content}],
            model_id=flagship_id,
            max_tokens=4000,
            thinking_budget=8000,
            timeout=180,
        )
        await wait.edit_text(f"🍬 <b>Стратегический совет</b>\n\n{answer}", parse_mode="HTML")
    except Exception as e:
        logger.error("cmd_strategy: %s", e)
        await wait.edit_text(f"⚠️ Ошибка при запросе к модели: {e}")


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pin — интерактивное описание группы с навигацией по разделам."""
    await update.message.reply_text(
        PIN_MAIN_TEXT, parse_mode="HTML",
        reply_markup=pin_main_keyboard(),
        disable_web_page_preview=True,
    )


async def on_pin_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Навигация по разделам /pin."""
    query = update.callback_query
    await query.answer()
    key = query.data.split("::", 1)[1]
    if key == "menu":
        await query.edit_message_text(
            PIN_MAIN_TEXT, parse_mode="HTML",
            reply_markup=pin_main_keyboard(),
            disable_web_page_preview=True,
        )
        return
    section = PIN_SECTIONS.get(key)
    if not section:
        return
    await query.edit_message_text(
        section[1], parse_mode="HTML",
        reply_markup=pin_back_keyboard(),
        disable_web_page_preview=True,
    )


# ------------------------------------------------------------------
# МЕНЮ ДЕЙСТВИЙ (/menu): выбрал действие → выбрал кого → бот ответил
# ------------------------------------------------------------------
MENU_ACTIONS = [
    ("priority", "🎯 Приоритетная задача"),
    ("plan", "🗂 Напомнить план"),
    ("status", "📋 Статусы задач"),
]
MENU_PERSONS = [("l", "Лена"), ("g", "Глеб"), ("b", "Лена + Глеб")]
PERSON_BY_CODE = {code: name for code, name in MENU_PERSONS}
MENU_TEXT = "🧭 <b>Меню</b>\nВыберите действие:"


def menu_home_keyboard():
    rows = [[InlineKeyboardButton(label, callback_data=f"menu::act::{key}")]
            for key, label in MENU_ACTIONS]
    rows.append([InlineKeyboardButton("➕ Новая задача", callback_data="newtask")])
    rows.append([InlineKeyboardButton("🆔 ID этого чата", callback_data="menu::chatid")])
    return InlineKeyboardMarkup(rows)


def menu_person_keyboard(action):
    rows = [[InlineKeyboardButton(name, callback_data=f"menu::run::{action}::{code}")
             for code, name in MENU_PERSONS]]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu::home")])
    return InlineKeyboardMarkup(rows)


def _back_to_menu_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ В меню", callback_data="menu::home")]]
    )


def _person_match(who, person):
    """Подходит ли исполнитель строки выбранному человеку (Лена + Глеб = все)."""
    if person == "Лена + Глеб":
        return True
    if person == "Лена":
        return who in ("Лена", "Лена + Глеб")
    if person == "Глеб":
        return who in ("Глеб", "Лена + Глеб")
    return who == person


def _deadline_sort_key(deadline):
    """Срочность дедлайна: меньше — приоритетнее. Backlog/непонятное — в конец."""
    norm = parse_deadline(deadline or "")
    if not norm:
        return 10 ** 6
    dd, mm = norm.split(".")
    today = datetime.datetime.now(TZINFO).date()
    try:
        d = datetime.date(today.year, int(mm), int(dd))
    except ValueError:
        return 10 ** 6
    delta = (d - today).days
    if delta < -180:   # дата сильно в прошлом — считаем, что это следующий год
        delta += 365
    return delta


def _tasks_for(person, groups):
    """Список (who, task) для человека, отсортированный по срочности дедлайна."""
    items = [(who, t) for who, tasks in groups.items()
             if _person_match(who, person) for t in tasks]
    items.sort(key=lambda it: _deadline_sort_key(it[1]["deadline"]))
    return items


def _format_status_for(person, groups):
    items = _tasks_for(person, groups)
    if not items:
        return f"🎉 У «{person}» открытых задач нет."
    lines = [f"📋 Открытые задачи — {person}", ""]
    for who, t in items:
        title = t["title"] or "(без названия)"
        extra = f"  ·  👤 {who}" if person == "Лена + Глеб" else ""
        lines.append(f"{t['status']}  {title}  ·  🗓️ {t['deadline']}{extra}")
    lines += ["", f"Итого: {len(items)}"]
    return "\n".join(lines)


def _format_priority_for(person, groups):
    items = _tasks_for(person, groups)
    if not items:
        return f"🎉 У «{person}» нет открытых задач — и приоритетов тоже."
    who, top = items[0]
    title = html.escape(top["title"] or "(без названия)")
    lines = [f"🎯 <b>Приоритет — {html.escape(person)}</b>", "",
             f"{top['status']}  <b>{title}</b>",
             f"🗓️ Дедлайн: {top['deadline']}"]
    if person == "Лена + Глеб":
        lines.append(f"👤 {html.escape(who)}")
    rest = items[1:3]
    if rest:
        lines.append("")
        lines.append("Следующие на очереди:")
        for w, t in rest:
            lines.append(f"• {t['status']}  {html.escape(t['title'])}  ·  🗓️ {t['deadline']}")
    return "\n".join(lines)


async def _gpt_plan_person(person, transcript, model_id):
    """План работы только для одного человека (для кнопки меню)."""
    now = datetime.datetime.now(TZINFO)
    sys_prompt = (
        "Ты — ассистент-планировщик небольшой команды (Лена и Глеб), проект Pastila OS. "
        f"На основе переписки составь КОНКРЕТНЫЙ план работы ТОЛЬКО для: {person}. "
        "По пунктам, по приоритету (сначала важное), кратко, без воды; сроки — если "
        f"упоминались. Начни строкой «📋 План для {person}:». Обычный текст, без markdown. "
        "Если задач для этого человека нет — так и напиши."
    )
    user = f"Сегодня {now:%d.%m.%Y}.\nПереписка (имя: текст):\n{transcript}"
    return await llm.call_llm(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user}],
        model_id, temperature=0.2, max_tokens=1500,
    )


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu — панель быстрых действий (приоритет, план, статусы) с выбором человека."""
    await update.message.reply_text(
        MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
    )


async def _run_menu_action(query, context, action, person):
    back = _back_to_menu_keyboard()
    chat_id = query.message.chat_id

    if action in ("status", "priority"):
        try:
            groups = await asyncio.to_thread(read_open_tasks)
        except Exception as e:
            logger.error("Меню (%s): чтение таблицы: %s", action, e)
            await query.edit_message_text(
                "⚠️ Не получилось прочитать таблицу. Попробуй позже.", reply_markup=back
            )
            return
        if action == "status":
            await query.edit_message_text(_format_status_for(person, groups), reply_markup=back)
        else:
            await query.edit_message_text(
                _format_priority_for(person, groups), parse_mode="HTML", reply_markup=back
            )
        return

    if action == "plan":
        if not llm.OPENROUTER_API_KEY:
            await query.edit_message_text(
                "🔇 Планировщик выключен — не задан OPENROUTER_API_KEY.", reply_markup=back
            )
            return
        buf = _CHAT_LOG.get(chat_id, [])
        transcript = await llm.prepare_context(buf, chat_id, task="plan")
        model_id, label, is_auto = llm.resolve_model(chat_id, "plan", transcript)
        await query.edit_message_text(
            f"🗂 Собираю план для «{person}» ({label}{' · авто' if is_auto else ''})…"
        )
        try:
            plan = (await _gpt_plan(transcript, "", model_id)) if person == "Лена + Глеб" \
                else (await _gpt_plan_person(person, transcript, model_id))
        except Exception as e:
            logger.error("Меню/план: %s", e)
            await query.edit_message_text("⚠️ Не получилось составить план.", reply_markup=back)
            return
        for ch in _chunks(plan.strip() or "Пусто."):
            await query.message.reply_text(ch)
        await query.edit_message_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
        )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Навигация по /menu: действие → для кого → результат."""
    query = update.callback_query
    parts = query.data.split("::")
    await query.answer()
    if len(parts) >= 2 and parts[1] == "open":
        # из приветствия (фото нельзя превратить в текст) — шлём меню отдельным сообщением
        await query.message.reply_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
        )
        return
    if len(parts) >= 2 and parts[1] == "home":
        await query.edit_message_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
        )
        return
    if len(parts) >= 2 and parts[1] == "chatid":
        chat = query.message.chat
        thread_id = query.message.message_thread_id
        lines = ["🆔 <b>ID этого чата</b>", "", f"<code>GROUP_CHAT_ID = {chat.id}</code>"]
        if thread_id is not None:
            lines.append(f"id этого топика = <code>{thread_id}</code>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_to_menu_keyboard()
        )
        return
    if len(parts) >= 3 and parts[1] == "act":
        action = parts[2]
        label = dict(MENU_ACTIONS).get(action, "Действие")
        await query.edit_message_text(
            f"{label}\n\nДля кого?", reply_markup=menu_person_keyboard(action)
        )
        return
    if len(parts) >= 4 and parts[1] == "run":
        action, code = parts[2], parts[3]
        person = PERSON_BY_CODE.get(code, "Лена + Глеб")
        await _run_menu_action(query, context, action, person)


# ------------------------------------------------------------------
# ЗАПУСК
# ------------------------------------------------------------------
async def _set_commands(app):
    """Регистрирует меню команд — всплывает подсказкой при вводе «/» в чате."""
    await app.bot.set_my_commands(
        [
            BotCommand("new", "Создать задачу"),
            BotCommand("menu", "Меню действий"),
            BotCommand("list", "Открытые задачи"),
            BotCommand("status", "Сменить статус (ответом на задачу)"),
            BotCommand("digest", "Дедлайны на сегодня"),
            BotCommand("alerts", "Дедлайны на завтра"),
            BotCommand("analyze", "Найти задачи в переписке"),
            BotCommand("plan", "План работы для Лены и Глеба"),
            BotCommand("q", "Быстро сохранить идею в базу знаний"),
            BotCommand("dash", "Сводка: задачи, просрочены, KB"),
            BotCommand("post", "Черновик поста для соцсетей"),
            BotCommand("recurring", "Повторяющиеся задачи"),
            BotCommand("remind_when", "Триггерное напоминание"),
            BotCommand("purge", "Удалить все задачи из таблицы (с подтверждением)"),
            BotCommand("deep", "Глубокий анализ всего: задачи + KB + проблемы + план"),
            BotCommand("strategy", "Стратегический совет — что делать дальше (Opus + thinking)"),
            BotCommand("pin", "Интерактивное описание группы с навигацией"),
            BotCommand("ai", "Проверить связь с OpenAI / OpenRouter"),
            BotCommand("session", "Добавить сессию/лог в базу знаний"),
            BotCommand("find", "Найти в базе знаний по теме или тегу"),
            BotCommand("notion", "Синхронизировать Notion → база знаний"),
            BotCommand("kb", "Показать базу знаний"),
            BotCommand("id", "ID этого чата"),
            BotCommand("cancel", "Отменить создание задачи"),
            BotCommand("start", "Что умеет бот"),
            BotCommand("help", "Что умеет бот"),
        ]
        + llm.COMMANDS
    )


def main():
    # подсказка в логах: чего ещё не хватает для полноценной работы
    missing = [
        n for n in ("GROUP_CHAT_ID", "THREAD_LENA", "THREAD_GLEB", "SHEET_ID")
        if not os.environ.get(n)
    ]
    if missing:
        logger.warning(
            "Не заданы переменные: %s. Бот запустится, но постинг и таблица не заработают. "
            "Запусти /id в нужных топиках, узнай id и добавь переменные в Render.",
            ", ".join(missing),
        )

    async def _post_init(application):
        await _set_commands(application)
        # Отправляем /pin в группу при каждом старте — обновляет закреплённое сообщение
        if GROUP_CHAT_ID:
            try:
                await application.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=PIN_MAIN_TEXT,
                    parse_mode="HTML",
                    reply_markup=pin_main_keyboard(),
                    disable_web_page_preview=True,
                )
                logger.info("Стартовый /pin отправлен в группу.")
            except Exception as e:
                logger.warning("Не смог отправить стартовый /pin: %s", e)

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
            CallbackQueryHandler(cmd_new_cb, pattern="^newtask$"),
        ],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            DOD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_dod),
                CallbackQueryHandler(skip_step, pattern="^skip::"),
            ],
            WHO: [CallbackQueryHandler(get_who_button, pattern="^who::")],
            DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_deadline),
                CallbackQueryHandler(skip_step, pattern="^skip::"),
            ],
            STEPS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_steps),
                CallbackQueryHandler(skip_step, pattern="^skip::"),
            ],
            MATERIALS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_materials),
                MessageHandler(filters.Document.ALL | filters.PHOTO, get_materials_file),
                CallbackQueryHandler(skip_step, pattern="^skip::"),
            ],
            TAGS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_tags),
                CallbackQueryHandler(skip_step, pattern="^skip::"),
            ],
            STATUS: [CallbackQueryHandler(get_status_and_publish, pattern="^status::")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("welcome", cmd_welcome))
    app.add_handler(CommandHandler("id", cmd_id))
    # бота добавили в группу → приветственный баннер
    app.add_handler(ChatMemberHandler(on_added_to_group, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_welcome_info, pattern="^welcomeinfo::"))
    app.add_handler(CallbackQueryHandler(on_help_nav, pattern="^help::"))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ai", cmd_ai_check))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CallbackQueryHandler(on_purge_confirm, pattern="^purge::"))
    app.add_handler(CommandHandler("q", cmd_quick_idea))
    app.add_handler(CommandHandler("dash", cmd_dash))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("recurring", cmd_recurring))
    app.add_handler(CommandHandler("remind_when", cmd_remind_when))
    app.add_handler(CallbackQueryHandler(on_alert_action, pattern="^alert::"))
    app.add_handler(CallbackQueryHandler(on_save_memo, pattern="^savememo::"))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("strategy", cmd_strategy))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CallbackQueryHandler(on_pin_nav, pattern="^pin::"))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("notion", cmd_notion_sync))
    app.add_handler(CommandHandler("kb", cmd_knowledge))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^menu::"))
    app.add_handler(CallbackQueryHandler(on_set_status, pattern="^setstatus::"))
    app.add_handler(CallbackQueryHandler(on_quick_status, pattern="^quick::"))
    app.add_handler(CallbackQueryHandler(on_voice_action, pattern="^voice::"))
    app.add_handler(CallbackQueryHandler(on_voice_status, pattern="^voicestatus::"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(conv)
    # импорт истории: result.json экспорта Telegram (вне диалога /new)
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), on_history_import))
    # файлы в группе → база знаний (group=1 — не мешает диалогу /new)
    app.add_handler(
        MessageHandler(filters.Document.ALL | filters.PHOTO, on_file), group=1
    )
    # фоновый сбор сообщений для /analyze — отдельная группа, чтобы не мешать диалогу
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_log_message), group=1)

    if not OPENAI_API_KEY:
        logger.info("Голосовой ввод выключен (не задан OPENAI_API_KEY).")

    # ежедневные задания: дайджест (дедлайн сегодня) и алерты (дедлайн завтра)
    if app.job_queue is not None:
        app.job_queue.run_daily(
            send_digest,
            time=datetime.time(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, tzinfo=TZINFO),
        )
        app.job_queue.run_daily(
            send_deadline_alerts,
            time=datetime.time(hour=ALERT_HOUR, minute=ALERT_MINUTE, tzinfo=TZINFO),
        )
        # Еженедельный отчёт — пятница 17:00
        app.job_queue.run_daily(
            send_weekly_report,
            time=datetime.time(hour=17, minute=0, tzinfo=TZINFO),
            days=(4,),  # 4 = пятница
        )
        # Проверка повторяющихся задач — каждый день в 09:00
        app.job_queue.run_daily(
            _check_recurring,
            time=datetime.time(hour=9, minute=0, tzinfo=TZINFO),
        )
        logger.info(
            "Запланировано: дайджест %02d:%02d, алерты %02d:%02d (%s)",
            DIGEST_HOUR, DIGEST_MINUTE, ALERT_HOUR, ALERT_MINUTE, TIMEZONE,
        )
    else:
        logger.warning(
            "JobQueue недоступен — дайджест и алерты не запланированы. Установи extra: "
            "python-telegram-bot[job-queue]"
        )

    _load_llm_state()
    _load_knowledge()
    llm.set_persistence(lambda: asyncio.to_thread(_save_llm_state))
    llm.register(app)
    llm.register_runner("analyze", _run_analyze)
    llm.register_runner("plan", _run_plan)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
