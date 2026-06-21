"""
Pastila OS — Task Bot
Заводит задачи через диалог с кнопками, постит в нужный топик группы и пишет в Google Sheets.
"""

import os
import re
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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
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
QUICK_STATUS = [("WIP", "🔵 В работу"), ("REVIEW", "🟣 Ревью"), ("DONE", "✅ Done")]
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
    return InlineKeyboardMarkup([buttons])


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
    """На шаге материалов прислали файл/фото — сохраняем ссылку на это сообщение."""
    msg = update.message
    link = message_link(msg.chat_id, msg.message_id, msg.message_thread_id)
    if msg.document and msg.document.file_name:
        context.user_data["materials"] = f"{msg.document.file_name} — {link}"
    else:
        context.user_data["materials"] = link
    await msg.reply_text(
        "📎 Принял файл. 🏷️ Теги (например: #excel #клиент #баг).\n\n"
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

    targets = []
    if who == "Лена":
        targets = [THREAD_LENA]
    elif who == "Глеб":
        targets = [THREAD_GLEB]
    elif who == "Лена + Глеб":
        targets = [THREAD_LENA, THREAD_GLEB]

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
            logger.error("Ошибка постинга в топик %s: %s", thread_id, e)

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
VOICE_SYSTEM_PROMPT = (
    "Ты помощник, который из расшифровки голосового сообщения извлекает поля задачи "
    "для таск-трекера небольшой команды (Лена и Глеб). "
    "Верни СТРОГО JSON-объект без пояснений с полями: "
    'title (короткое название), dod (критерий готовности или ""), '
    'who (одно из: "Лена", "Глеб", "Лена + Глеб"; если не ясно — ""), '
    'deadline (формат ДД.ММ или ""; относительные даты вычисляй от сегодняшней), '
    'steps (массив строк), materials (строка или ""), tags (массив слов без #), '
    "status (одно из: NEW, TODO, WIP, WAITING, REVIEW, DONE, BLOCKED, CANCELLED; "
    "если статус в речи не назван явно — верни NEW)."
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


async def _gpt_parse(transcript):
    """Раскладывает расшифровку по полям задачи через OpenAI. Возвращает dict."""
    now = datetime.datetime.now(TZINFO)
    user = f"Сегодня {now:%d.%m.%Y}. Расшифровка голосового:\n{transcript}"
    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": VOICE_SYSTEM_PROMPT},
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
        parsed = await _gpt_parse(transcript)
        data = _draft_from_parsed(parsed)
    except Exception as e:
        logger.error("Голос: ошибка обработки: %s", e)
        await status_msg.edit_text("⚠️ Не получилось обработать голосовое. Заведи через /new.")
        return

    preview = transcript.strip()
    if len(preview) > 250:
        preview = preview[:250] + "…"
    draft = f"🎙️ Услышал: «{preview}»\n\n— Черновик —\n{build_task_text(data)}\n\nОпубликовать?"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data="voice::publish"),
        InlineKeyboardButton("❌ Отмена", callback_data="voice::cancel"),
    ]])
    sent = await status_msg.edit_text(draft, reply_markup=kb)
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я бот для задач Pastila OS.\n\n"
        "Команды:\n"
        "/new — создать задачу\n"
        "/list — открытые задачи\n"
        "/status — сменить статус (в ответ на задачу)\n"
        "/digest — дайджест дедлайнов на сегодня\n"
        "/alerts — алерты по дедлайнам на завтра\n"
        "/id — узнать chat_id и id топика (для настройки)\n"
        "/cancel — отменить\n\n"
        "🎙️ Или пришли голосовое — соберу задачу из него."
    )


# ------------------------------------------------------------------
# ЗАПУСК
# ------------------------------------------------------------------
async def _set_commands(app):
    """Регистрирует меню команд — всплывает подсказкой при вводе «/» в чате."""
    await app.bot.set_my_commands(
        [
            BotCommand("new", "Создать задачу"),
            BotCommand("list", "Открытые задачи"),
            BotCommand("status", "Сменить статус (ответом на задачу)"),
            BotCommand("digest", "Дедлайны на сегодня"),
            BotCommand("alerts", "Дедлайны на завтра"),
            BotCommand("cancel", "Отменить создание задачи"),
            BotCommand("start", "Справка"),
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
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CallbackQueryHandler(on_set_status, pattern="^setstatus::"))
    app.add_handler(CallbackQueryHandler(on_quick_status, pattern="^quick::"))
    app.add_handler(CallbackQueryHandler(on_voice_action, pattern="^voice::"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(conv)

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
