#!/bin/zsh
# menu.sh <chat_id> <screen>
# Нижняя сетка (reply-клавиатура). Принцип: разделы = ПЕРЕХОДЫ (появляется больше кнопок, без простыней),
# ИНФО-текст выдаётся только на ЛИСТЕ (конкретная модель/бот/функция/плагин/коннектор).
# Токен из ~/.claude/channels/telegram/.env (не печатается).

CHAT_ID="$1"; SCREEN="${2:-home}"
python3 - "$CHAT_ID" "$SCREEN" <<'PY'
import json, os, sys, urllib.request
chat_id, screen = sys.argv[1], sys.argv[2]
env={}
with open(os.path.expanduser("~/.claude/channels/telegram/.env")) as f:
    for line in f:
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); env[k]=v
token=env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN") or env.get("TOKEN")
try: cur=open(os.path.expanduser("~/pastila_bot/.bridge-model")).read().strip()
except Exception: cur="opus"
mn={"sonnet":"🍏 Sonnet 4.6","opus":"🍎 Opus 4.8","fable":"🍓 Fable 5"}
def mmark(k): return mn[k]+(" ✓" if k==cur else "")

# НАВИГАЦИОННЫЕ экраны: короткий заголовок + кнопки (переходы). BACK добавляется автоматически (кроме home).
NAV={
 "home":("☰ МЕНЮ — выбери раздел:",
     [["🍎 Доступные модели","🍫 Больше ботов"],["ℹ️ Про этот бот","🔌 Доступные плагины"],
      ["⚙️ Функции","🔗 Коннекторы"]], None, "☰ Меню — выбери раздел…"),
 "models":("🍎 Доступные модели — выбери (✓ активна сейчас):",
     [[mmark("sonnet"),mmark("opus")],[mmark("fable")]], "⬅️ Назад в меню", "Выбери модель…"),
 "bots":("🍫 Больше ботов — выбери бота:",
     [["🍎 Бридж (Claude)","🗂 Task-бот"],["🤖 GPT-помощник"]], "⬅️ Назад в меню", "Выбери бота…"),
 "about":("ℹ️ Про этот бот — выбери:",
     [["👑 Владелец","🧁 Велком"],["❓ Помощь","🍬 Показать ID"]], "⬅️ Назад в меню", "Выбери…"),
 "plugins":("🔌 Доступные плагины — выбери, чтобы узнать что это:",
     [["🔗 Telegram","🎨 Frontend-design"],["🗂 Atlassian"]], "⬅️ Назад в меню", "Выбери плагин…"),
 "functions":("⚙️ Функции — выбери, чтобы узнать что делает:",
     [["🍪 Разбор","✂️ Саммари"],["🗺 План","🎯 Приоритеты"],["✅ Задачи","⚠️ Риски"],["📊 Цифры","💡 Советы"]],
     "⬅️ Назад в меню", "Выбери функцию…"),
 "connectors":("🔗 Коннекторы — выбери, чтобы узнать что это:",
     [["📓 Notion","📁 Drive"],["✉️ Gmail","📅 Calendar"],["✅ Asana"]], "⬅️ Назад в меню", "Выбери коннектор…"),
}
# ЛИСТЬЯ: инфо-текст + [назад к родителю]+[☰ Меню]. (доп. кнопки — extra перед возвратом)
def leaf(text, back_label, extra=None):
    return {"text":text,"back":back_label,"extra":extra or []}
BOTADD={"botbridge":("бридж","pastila_code_remote_bot"),"bottask":("Task","PastilaTaskBot"),
        "botgpt":("GPT","pastila_gPT_remote_bot")}
LEAF={
 # модели (переключение — кнопкой, если не активна)
 "msonnet":leaf("🍏 Sonnet 4.6 — быстрая и лёгкая. Лучше для: повседневных вопросов, коротких текстов, простых правок, быстрых ответов. Слабее на очень сложном.","⬅️ К моделям", None if cur=="sonnet" else [["🔄 Переключить на Sonnet"]]),
 "mopus":leaf("🍎 Opus 4.8 — самая умная и вдумчивая. Лучше для: глубокого анализа, кода, больших разборов, стратегии. Чуть медленнее.","⬅️ К моделям", None if cur=="opus" else [["🔄 Переключить на Opus"]]),
 "mfable":leaf("🍓 Fable 5 — с уклоном в тексты и креатив. Лучше для: живых художественных текстов, сторителлинга, постов. Не для кода/точности.","⬅️ К моделям", None if cur=="fable" else [["🔄 Переключить на Fable"]]),
 # боты (описание + добавить в группу)
 "botbridge":leaf("🍎 БРИДЖ (Claude Code) — @pastila_code_remote_bot\nГлавный ассистент канала: разбор файлов и экспортов Claude, Notion/Drive/Gmail/Calendar, картинки, тексты, задачи, смена модели.","⬅️ К ботам",[["➕ Добавить бридж в группу"]]),
 "bottask":leaf("🗂 TASK-БОТ — @PastilaTaskBot\nЗадачи и напоминания: /new, /list, /digest, /alerts. Держит общую таблицу команды.","⬅️ К ботам",[["➕ Добавить Task в группу"]]),
 "botgpt":leaf("🤖 GPT-ПОМОЩНИК — @pastila_gPT_remote_bot\nСвободный чат с GPT: напиши вопрос. /start, /help.","⬅️ К ботам",[["➕ Добавить GPT в группу"]]),
 # про бот
 "owner":leaf("👑 ВЛАДЕЛЕЦ — Лена (в прошлом @elenaisanewleet, сейчас @sorrbouthat). По вопросам и доступам — к ней.","⬅️ К «Про этот бот»"),
 "help":leaf("❓ ПОМОЩЬ — напиши задачу словами или пришли файл. Разделы — кнопками, «⬅️ Назад» возвращает выше. Полный список команд — «/» в поле.","⬅️ К «Про этот бот»"),
 # плагины
 "plg_tg":leaf("🔗 Telegram — плагин связи бота с этим чатом: приём сообщений, ответы, кнопки, файлы.","⬅️ К плагинам"),
 "plg_fd":leaf("🎨 Frontend-design — помощь с дизайном интерфейсов и вёрсткой.","⬅️ К плагинам"),
 "plg_atl":leaf("🗂 Atlassian — работа с Jira и Confluence: задачи и документация.","⬅️ К плагинам"),
 # функции
 "fn_analyze":leaf("🍪 Разбор — полный разбор материала: что это, ключевое по пунктам, выводы. Пришли файл и нажми, или напиши «разбери».","⬅️ К функциям"),
 "fn_summary":leaf("✂️ Саммари — краткая выжимка сути (5–8 предложений), без воды.","⬅️ К функциям"),
 "fn_plan":leaf("🗺 План — пошаговый план действий: что делать, кто, срок.","⬅️ К функциям"),
 "fn_priority":leaf("🎯 Приоритеты — топ-3–5 самого важного и срочного с обоснованием.","⬅️ К функциям"),
 "fn_tasks":leaf("✅ Задачи — конкретные задачи из материала; можно завести в Task-бот.","⬅️ К функциям"),
 "fn_risks":leaf("⚠️ Риски — проблемы, узкие места, на что обратить внимание.","⬅️ К функциям"),
 "fn_numbers":leaf("📊 Цифры — итоги по таблицам: суммы, средние, аномалии.","⬅️ К функциям"),
 "fn_advice":leaf("💡 Советы — конкретные рекомендации и следующие шаги.","⬅️ К функциям"),
 # коннекторы
 "cn_notion":leaf("📓 Notion — база знаний и страницы. Пример: «возьми из Notion страницу X».","⬅️ К коннекторам"),
 "cn_drive":leaf("📁 Google Drive — файлы и документы. Пример: «найди на Диске отчёт».","⬅️ К коннекторам"),
 "cn_gmail":leaf("✉️ Gmail — почта. Пример: «найди письмо от …».","⬅️ К коннекторам"),
 "cn_cal":leaf("📅 Google Calendar — события и расписание. Пример: «что в календаре на завтра».","⬅️ К коннекторам"),
 "cn_asana":leaf("✅ Asana — задачи проектов.","⬅️ К коннекторам"),
}
def t(x): return {"text":x}
if screen in NAV:
    title,rows,back,ph=NAV[screen]
    kb=[[t(x) for x in row] for row in rows]
    if back: kb.append([t(back)])
    text=title
elif screen in LEAF:
    lf=LEAF[screen]; text=lf["text"]
    kb=[[t(x) for x in row] for row in lf["extra"]]
    kb.append([t(lf["back"]),t("☰ Меню")])
    ph="⬅️ Назад"
else:
    title,rows,back,ph=NAV["home"]; kb=[[t(x) for x in row] for row in rows]; text=title
km={"keyboard":kb,"resize_keyboard":True,"is_persistent":True,"input_field_placeholder":ph}
body={"chat_id":chat_id,"text":text,"reply_markup":json.dumps(km)}
req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req,timeout=30))
print("menu '"+screen+"' sent:", d.get("ok"))
PY
