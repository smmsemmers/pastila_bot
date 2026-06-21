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

import httpx
import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    BotCommand,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# ------------------------------------------------------------------
# GOOGLE SHEETS — подключение
# ------------------------------------------------------------------
def get_worksheet():
    """Открывает лист Google-таблицы через сервисный ключ."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    # Ключ сервисного аккаунта кладём в переменную окружения GOOGLE_CREDENTIALS (весь JSON одной строкой)
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


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
) = range(8)

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
    lines.append(f"📌 ЗАДАЧА: {data.get('title', '')}")
    if data.get("dod"):
        lines.append(f"🏁 DoD: {data['dod']}")
    lines.append("———————————")
    lines.append(f"👤 КТО: {data.get('who', '')}")
    lines.append(f"🗓️ ДЕДЛАЙН: {data.get('deadline', 'Backlog')}")
    lines.append("———————————")
    if data.get("steps"):
        lines.append("📋 ЧТО СДЕЛАТЬ")
        for step in data["steps"].split("\n"):
            step = step.strip()
            if step:
                lines.append(f"   ✦ {step}")
        lines.append("———————————")
    materials = data.get("materials") or "—"
    lines.append(f"📎 МАТЕРИАЛЫ: {materials}")
    if data.get("tags"):
        lines.append(f"🏷️ ТЕГИ: {data['tags']}")
    lines.append("———————————")
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
        body = f"⏰ Дедлайн завтра ({tomorrow})!\n{t['status']}  {t['title']}"
        for thread_id, tag in targets:
            try:
                await bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    message_thread_id=thread_id,
                    text=f"{tag}\n{body}",
                )
                count += 1
            except Exception as e:
                logger.error("Алерт: не смог отправить в топик %s: %s", thread_id, e)
    return count


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
    "Тебе дают расшифровку голосового сообщения и недавнюю переписку команды (Лена, Глеб). "
    "Определи намерение и верни СТРОГО JSON.\n"
    "• Если голосовое — это просьба ЗАВЕСТИ/СОЗДАТЬ задачу, поручение, напоминание что-то сделать — "
    'верни {"type":"task","title":...,"dod":"","who":"","deadline":"","steps":[],"materials":"","tags":[],"status":"NEW"} '
    "и заполни поля ТОЛЬКО из голосового (переписку для задачи игнорируй). "
    'who — «Лена»/«Глеб»/«Лена + Глеб»/""; deadline — ДД.ММ или "" (относительные даты считай от сегодня); '
    "tags — слова без #; status — NEW, если в речи статус не назван.\n"
    "• Если голосовое — это ВОПРОС или ПОИСК по переписке («найди где…», «что Глеб говорил про…», "
    "«когда…», «покажи…») — найди ответ В ПЕРЕПИСКЕ и верни "
    '{"type":"answer","text":"<краткий ответ с цитатами и кто что сказал; если в переписке нет — честно скажи>"}.'
)


async def _whisper_transcribe(audio_bytes):
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


async def _gpt_voice_route(transcript, chat_log):
    """По голосовому решает: завести задачу или ответить на вопрос по переписке.
    Возвращает dict с ключом type: "task" (+поля задачи) или "answer" (+text)."""
    now = datetime.datetime.now(TZINFO)
    log_text = "\n".join(f"{n}: {t}" for n, t in chat_log[-200:]) or "(переписки пока нет)"
    user = f"Сегодня {now:%d.%m.%Y}.\n\nГолосовое: {transcript}\n\nПереписка (имя: текст):\n{log_text}"
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": VOICE_ROUTER_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    return json.loads(content)


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
    """Голосовое → распознаём → собираем черновик задачи на подтверждение."""
    if not OPENAI_API_KEY:
        return  # фича выключена (нет ключа) — молчим
    msg = update.message
    status_msg = await msg.reply_text("🎙️ Слушаю и распознаю…")
    try:
        tg_file = await msg.voice.get_file()
        audio = await tg_file.download_as_bytearray()
        transcript = await _whisper_transcribe(bytes(audio))
        if not transcript.strip():
            await status_msg.edit_text("🤔 Не разобрал речь. Попробуй ещё раз или /new.")
            return
        chat_log = _CHAT_LOG.get(msg.chat_id, [])
        result = await _gpt_voice_route(transcript, chat_log)
        # это вопрос/поиск по переписке — отвечаем, задачу не заводим
        if result.get("type") == "answer":
            answer = (result.get("text") or "").strip() or "В переписке ничего не нашёл."
            await status_msg.edit_text(f"🔎 {answer}")
            return
        data = _draft_from_parsed(result)
    except Exception as e:
        logger.error("Голос: ошибка обработки: %s", e)
        await status_msg.edit_text("⚠️ Не получилось обработать голосовое. Заведи через /new.")
        return

    preview = transcript.strip()
    if len(preview) > 250:
        preview = preview[:250] + "…"
    draft = (
        f"🎙️ Услышал: «{preview}»\n\n— Черновик —\n{build_task_text(data)}\n\n"
        "🚦 Выбери статус — и задача опубликуется:"
    )
    # выбор статуса в конце (как в /new) + отмена
    rows = [list(row) for row in status_keyboard("voicestatus").inline_keyboard]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="voice::cancel")])
    sent = await status_msg.edit_text(draft, reply_markup=InlineKeyboardMarkup(rows))
    # черновик храним в chat_data под id сообщения (переживёт смену пользователя, но не рестарт)
    context.chat_data.setdefault("voice_drafts", {})[sent.message_id] = data


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


async def on_log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тихо копит текстовые сообщения чата для последующего анализа (/analyze)."""
    msg = update.message
    if not msg or not msg.text or msg.text.startswith("/"):
        return
    name = msg.from_user.full_name if msg.from_user else "?"
    buf = _CHAT_LOG.setdefault(msg.chat_id, [])
    buf.append((name, msg.text))
    if len(buf) > _LOG_MAX:
        del buf[: len(buf) - _LOG_MAX]


ANALYZE_SYSTEM_PROMPT = (
    "Ты анализируешь рабочую переписку небольшой команды (Лена и Глеб) по проекту "
    "Pastila OS. Найди КОНКРЕТНЫЕ задачи, поручения и явные пожелания/приоритеты — "
    "особенно то, что хочет или просит Глеб. Игнорируй болтовню и обсуждения без действия. "
    'Верни СТРОГО JSON: {"tasks": [ {title, who, deadline, dod, steps, tags, status}, ... ]}, '
    'максимум 5 самых конкретных. who — одно из «Лена»/«Глеб»/«Лена + Глеб»/""; '
    'deadline — ДД.ММ или ""; steps — массив строк; tags — массив слов без #; status всегда NEW. '
    'Если конкретных задач нет — верни {"tasks": []}.'
)


async def _gpt_analyze(transcript):
    """Просит модель найти задачи в переписке. Возвращает список dict."""
    now = datetime.datetime.now(TZINFO)
    user = f"Сегодня {now:%d.%m.%Y}. Переписка (имя: текст):\n{transcript}"
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions", headers=headers, json=payload
        )
        r.raise_for_status()
        obj = json.loads(r.json()["choices"][0]["message"]["content"])
    return obj.get("tasks", []) if isinstance(obj, dict) else []


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/analyze — разобрать накопленную переписку и предложить задачи."""
    if not OPENAI_API_KEY:
        await update.message.reply_text("🔇 Анализ выключен — не задан OPENAI_API_KEY.")
        return
    buf = _CHAT_LOG.get(update.message.chat_id, [])
    if len(buf) < 3:
        await update.message.reply_text(
            "Пока мало сообщений для анализа. Я вижу переписку только с момента запуска — "
            "пообщайтесь в чате и вызови /analyze позже."
        )
        return
    note = await update.message.reply_text("🧠 Анализирую переписку…")
    transcript = "\n".join(f"{n}: {t}" for n, t in buf[-500:])
    try:
        tasks = await _gpt_analyze(transcript)
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
            chat_id=update.message.chat_id,
            message_thread_id=update.message.message_thread_id,
            text=draft,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        context.chat_data.setdefault("voice_drafts", {})[sent.message_id] = data


def _chunks(text, size=4000):
    """Режет длинный текст на куски под лимит сообщения Telegram."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


PLAN_SYSTEM_PROMPT = (
    "Ты — ассистент-планировщик небольшой команды (Лена и Глеб), проект Pastila OS. "
    "На основе переписки (и доп. контекста, если он есть) составь КОНКРЕТНЫЙ план работы — "
    "отдельно для Лены и отдельно для Глеба. Для каждого: что сделать и в каком порядке "
    "(по приоритету), сроки — если упоминались. По пунктам, кратко, без воды. "
    "Формат:\n\n📋 План для Лены:\n1. …\n2. …\n\n📋 План для Глеба:\n1. …\n2. …\n\n"
    "Если по человеку задач нет — так и напиши. Обычный текст, без markdown-звёздочек."
)


async def _gpt_plan(transcript, extra=""):
    """Просит модель составить план работы для Лены и Глеба. Возвращает текст."""
    now = datetime.datetime.now(TZINFO)
    parts = [f"Сегодня {now:%d.%m.%Y}."]
    if extra:
        parts.append(f"Дополнительный контекст: {extra}")
    parts.append(f"Переписка (имя: текст):\n{transcript}")
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions", headers=headers, json=payload
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/plan — план работы для Лены и Глеба по переписке (+ необязательный текст после команды)."""
    if not OPENAI_API_KEY:
        await update.message.reply_text("🔇 Планировщик выключен — не задан OPENAI_API_KEY.")
        return
    extra = " ".join(context.args) if context.args else ""
    buf = _CHAT_LOG.get(update.message.chat_id, [])
    if len(buf) < 3 and not extra:
        await update.message.reply_text(
            "Маловато данных. Я вижу переписку только с момента запуска — пообщайтесь и вызови "
            "/plan позже, либо добавь контекст после команды, например: "
            "/plan что нужно успеть к запуску."
        )
        return
    note = await update.message.reply_text("🗂 Составляю план…")
    transcript = "\n".join(f"{n}: {t}" for n, t in buf[-500:])
    try:
        plan = await _gpt_plan(transcript, extra)
    except Exception as e:
        logger.error("План: %s", e)
        await note.edit_text("⚠️ Не получилось составить план. Попробуй позже.")
        return
    chunks = _chunks(plan.strip() or "Пусто.")
    await note.edit_text(chunks[0])
    for ch in chunks[1:]:
        await update.message.reply_text(ch)


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
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Подробнее — как всё работает", callback_data="welcomeinfo::how")]]
    )


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


async def _gpt_plan_person(person, transcript):
    """План работы только для одного человека (для кнопки меню)."""
    now = datetime.datetime.now(TZINFO)
    sys = (
        "Ты — ассистент-планировщик небольшой команды (Лена и Глеб), проект Pastila OS. "
        f"На основе переписки составь КОНКРЕТНЫЙ план работы ТОЛЬКО для: {person}. "
        "По пунктам, по приоритету (сначала важное), кратко, без воды; сроки — если "
        f"упоминались. Начни строкой «📋 План для {person}:». Обычный текст, без markdown. "
        "Если задач для этого человека нет — так и напиши."
    )
    user = f"Сегодня {now:%d.%m.%Y}.\nПереписка (имя: текст):\n{transcript}"
    payload = {
        "model": OPENAI_MODEL, "temperature": 0.2,
        "messages": [{"role": "system", "content": sys},
                     {"role": "user", "content": user}],
    }
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions", headers=headers, json=payload
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


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
        if not OPENAI_API_KEY:
            await query.edit_message_text(
                "🔇 Планировщик выключен — не задан OPENAI_API_KEY.", reply_markup=back
            )
            return
        await query.edit_message_text(f"🗂 Собираю план для «{person}»…")
        buf = _CHAT_LOG.get(chat_id, [])
        transcript = "\n".join(f"{n}: {t}" for n, t in buf[-500:])
        try:
            plan = (await _gpt_plan(transcript)) if person == "Лена + Глеб" \
                else (await _gpt_plan_person(person, transcript))
        except Exception as e:
            logger.error("Меню/план: %s", e)
            await query.edit_message_text("⚠️ Не получилось составить план.", reply_markup=back)
            return
        for ch in _chunks(plan.strip() or "Пусто."):
            await query.message.reply_text(ch)
        # возвращаем меню на место, чтобы можно было сделать ещё действие
        await query.edit_message_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
        )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Навигация по /menu: действие → для кого → результат."""
    query = update.callback_query
    parts = query.data.split("::")
    await query.answer()
    if len(parts) >= 2 and parts[1] == "home":
        await query.edit_message_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=menu_home_keyboard()
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
            BotCommand("id", "ID этого чата"),
            BotCommand("cancel", "Отменить создание задачи"),
            BotCommand("start", "Что умеет бот"),
            BotCommand("help", "Что умеет бот"),
        ]
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

    app = Application.builder().token(BOT_TOKEN).post_init(_set_commands).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
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
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^menu::"))
    app.add_handler(CallbackQueryHandler(on_set_status, pattern="^setstatus::"))
    app.add_handler(CallbackQueryHandler(on_quick_status, pattern="^quick::"))
    app.add_handler(CallbackQueryHandler(on_voice_action, pattern="^voice::"))
    app.add_handler(CallbackQueryHandler(on_voice_status, pattern="^voicestatus::"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(conv)
    # импорт истории: result.json экспорта Telegram (вне диалога /new)
    app.add_handler(MessageHandler(filters.Document.FileExtension("json"), on_history_import))
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
        logger.info(
            "Запланировано: дайджест %02d:%02d, алерты %02d:%02d (%s)",
            DIGEST_HOUR, DIGEST_MINUTE, ALERT_HOUR, ALERT_MINUTE, TIMEZONE,
        )
    else:
        logger.warning(
            "JobQueue недоступен — дайджест и алерты не запланированы. Установи extra: "
            "python-telegram-bot[job-queue]"
        )

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
