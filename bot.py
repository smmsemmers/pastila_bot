"""
Pastila OS — Task Bot
Заводит задачи через диалог с кнопками, постит в нужный топик группы и пишет в Google Sheets.
"""

import os
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


def status_keyboard():
    # по две кнопки в ряд, чтобы компактнее
    rows = []
    for i in range(0, len(STATUS_OPTIONS), 2):
        row = [
            InlineKeyboardButton(STATUS_OPTIONS[i], callback_data=f"status::{STATUS_OPTIONS[i]}")
        ]
        if i + 1 < len(STATUS_OPTIONS):
            row.append(
                InlineKeyboardButton(
                    STATUS_OPTIONS[i + 1], callback_data=f"status::{STATUS_OPTIONS[i + 1]}"
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


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я бот для задач Pastila OS.\n\n"
        "Команды:\n"
        "/new — создать задачу\n"
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
    app.add_handler(conv)

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
