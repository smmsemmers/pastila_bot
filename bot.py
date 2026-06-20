"""
Pastila OS — Task Bot
Заводит задачи через диалог с кнопками, постит в нужный топик группы и пишет в Google Sheets.
"""

import os
import asyncio
import logging
import datetime
import json

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
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
BOT_TOKEN = os.environ["BOT_TOKEN"]                 # токен от BotFather
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])    # id группы (отрицательное, начинается с -100)
THREAD_LENA = int(os.environ["THREAD_LENA"])        # message_thread_id топика Tasks — Лена
THREAD_GLEB = int(os.environ["THREAD_GLEB"])        # message_thread_id топика Tasks — Глеб
SHEET_ID = os.environ["SHEET_ID"]                   # id Google-таблицы
SHEET_NAME = os.environ.get("SHEET_NAME", "Sheet1") # имя листа

# Юзернеймы для тегов в задаче
TAG_LENA = os.environ.get("TAG_LENA", "@elenaisanewleet")
TAG_GLEB = os.environ.get("TAG_GLEB", "@foxruso")

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


def append_task_to_sheet(date_str, who, task_title, deadline, status):
    """Добавляет строку в таблицу: Дата | Кто | Задача | Дедлайн | Статус | Ссылка."""
    try:
        ws = get_worksheet()
        ws.append_row(
            [date_str, who, task_title, deadline, status, ""],
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
    context.user_data["deadline"] = update.message.text.strip()
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


async def get_status_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    status = query.data.split("::", 1)[1]
    context.user_data["status"] = status
    await query.edit_message_text(f"🚦 Статус: {status}")

    data = context.user_data
    # добавляем тег исполнителя в текст
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

    # определяем топик(и) куда постить
    targets = []
    if who == "Лена":
        targets = [THREAD_LENA]
    elif who == "Глеб":
        targets = [THREAD_GLEB]
    elif who == "Лена + Глеб":
        targets = [THREAD_LENA, THREAD_GLEB]

    # постим в группу
    for thread_id in targets:
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=thread_id,
                text=task_text,
            )
        except Exception as e:
            logger.error("Ошибка постинга в топик %s: %s", thread_id, e)

    # пишем в таблицу
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    sheet_ok = append_task_to_sheet(
        today, who, data.get("title", ""), data.get("deadline", ""), status
    )

    # подтверждение пользователю
    confirm = "✅ Задача создана и отправлена в топик."
    if sheet_ok:
        confirm += "\n📊 Записана в таблицу."
    else:
        confirm += "\n⚠️ В таблицу записать не удалось (проверь доступ)."
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я бот для задач Pastila OS.\n\n"
        "Команды:\n"
        "/new — создать задачу\n"
        "/list — открытые задачи\n"
        "/status — сменить статус (в ответ на задачу)\n"
        "/cancel — отменить"
    )


# ------------------------------------------------------------------
# ЗАПУСК
# ------------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_set_status, pattern="^setstatus::"))
    app.add_handler(conv)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
