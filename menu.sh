#!/bin/zsh
# menu.sh <chat_id> <screen>
# Нижняя сетка. Принцип (по ТЗ Лены): максимум НАВИГАЦИИ кнопками до конечного пункта.
# На пункте — сначала кнопки: «❓ Что это», действие (переключить/добавить), назад.
# ТЕКСТ-инфо выдаётся ТОЛЬКО по кнопке «❓ Что это» (экран <key>_info), под ним снова кнопки.
# Экраны NAV: home|models|bots|about|plugins|functions|connectors
# Экраны пунктов: <key> (кнопки) и <key>_info (текст). Токен из ~/.claude/channels/telegram/.env.

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
mn={"sonnet":"🍏 Sonnet 5","opus":"🍎 Opus 4.8","fable":"🍓 Fable 5"}
def mmark(k): return mn[k]+(" ✓" if k==cur else "")

# NAV: (заголовок, ряды кнопок, back_label|None, placeholder)
NAV={
 "home":("☰ МЕНЮ — выбери раздел:",
   [["🍎 Доступные модели","🍫 Больше ботов"],["🎛 Режимы","⚙️ Функции"],
    ["🔌 Доступные плагины","🔗 Коннекторы"],["ℹ️ Про этот бот"]],None,"☰ Меню…"),
 "modes":("🎛 Режимы — выбери, чтобы узнать как пользоваться:",
   [["💬 Чат","💻 Код"],["🎨 Дизайн","🧩 Cowork"]],"⬅️ Назад в меню","Выбери режим…"),
 "models":("🍎 Доступные модели (✓ активна). Выбери — покажу кнопки:",
   [[mmark("sonnet"),mmark("opus")],[mmark("fable"),"🍡 Haiku 4.5"],["⚡ Уровень (Effort)"]],"⬅️ Назад в меню","Выбери модель…"),
 "bots":("🍫 Больше ботов. Выбери бота:",
   [["🍎 Бридж (Claude)","🗂 Task-бот"],["🤖 GPT-помощник"]],"⬅️ Назад в меню","Выбери бота…"),
 "about":("ℹ️ Про этот бот. Выбери:",
   [["👑 Владелец","🏢 Про Anthropic"],["🧁 Велком","❓ Помощь"],["🍬 Показать ID"]],"⬅️ Назад в меню","Выбери…"),
 "plugins":("🔌 Доступные плагины. Выбери:",
   [["🔗 Telegram","🎨 Frontend-design"],["🗂 Atlassian"]],"⬅️ Назад в меню","Выбери плагин…"),
 "functions":("⚙️ Функции. Выбери:",
   [["🍪 Разбор","✂️ Саммари"],["🗺 План","🎯 Приоритеты"],["✅ Задачи","⚠️ Риски"],["📊 Цифры","💡 Советы"]],
   "⬅️ Назад в меню","Выбери функцию…"),
 "connectors":("🔗 Коннекторы. Выбери:",
   [["📓 Notion","📁 Drive"],["✉️ Gmail","📅 Calendar"],["✅ Asana"]],"⬅️ Назад в меню","Выбери коннектор…"),
}
# ITEMS: пункт-лист. name=заголовок компактного экрана, short=для кнопки «Что это: <short>»,
# info=текст, pl=подпись «назад к разделу», primary=подпись кнопки действия (или None)
def switch(k): return None if cur==k else "🔄 Переключить на "+{"sonnet":"Sonnet","opus":"Opus","fable":"Fable"}[k]
ITEMS={
 "mopus":(mn["opus"],"Opus","🍎 Opus 4.8 — для сложных задач: глубокий анализ, код, большие разборы, стратегия. Самая мощная в ядре.","⬅️ К моделям",switch("opus")),
 "msonnet":(mn["sonnet"],"Sonnet","🍏 Sonnet 5 — самая эффективная для повседневных задач: быстрые ответы, тексты, правки.","⬅️ К моделям",switch("sonnet")),
 "mfable":(mn["fable"],"Fable","🍓 Fable 5 — для самых сложных вызовов и креатива: живые тексты, нестандартные задачи.","⬅️ К моделям",switch("fable")),
 "mhaiku":("🍡 Haiku 4.5","Haiku","🍡 Haiku 4.5 — самая быстрая, для коротких быстрых ответов. В ядре бриджа пока не переключается (доступны Opus/Sonnet/Fable).","⬅️ К моделям",None),
 "mlevel":("⚡ Уровень (Effort)","Уровень","⚡ Уровень (Effort) — глубина «размышления»: выше → тщательнее и медленнее, ниже → быстрее. В вебе Claude выбирается рядом с моделью (сейчас High).","⬅️ К моделям",None),
 "botbridge":("🍎 Бридж (Claude)","Бридж","🍎 БРИДЖ (Claude Code) — @pastila_code_remote_bot. Главный ассистент: разбор файлов и экспортов Claude, Notion/Drive/Gmail/Calendar, картинки, тексты, задачи, смена модели.","⬅️ К ботам","➕ Добавить бридж в группу"),
 "bottask":("🗂 Task-бот","Task","🗂 TASK-БОТ — @PastilaTaskBot. Задачи и напоминания: /new, /list, /digest, /alerts. Держит общую таблицу команды.","⬅️ К ботам","➕ Добавить Task в группу"),
 "botgpt":("🤖 GPT-помощник","GPT","🤖 GPT-ПОМОЩНИК — @pastila_gPT_remote_bot. Свободный чат с GPT: напиши вопрос. /start, /help.","⬅️ К ботам","➕ Добавить GPT в группу"),
 "owner":("👑 Владелец","Владелец","👑 ВЛАДЕЛЕЦ — Лена (в прошлом @elenaisanewleet, сейчас @sorrbouthat). По вопросам и доступам — к ней.","⬅️ К «Про этот бот»",None),
 "help":("❓ Помощь","Помощь","❓ ПОМОЩЬ — напиши задачу словами или пришли файл. Разделы — кнопками, «⬅️ Назад» возвращает выше. Полный список команд — «/» в поле.","⬅️ К «Про этот бот»",None),
 "anthropic":("🏢 Про Anthropic","Anthropic","🏢 ANTHROPIC — компания, создавшая Claude. Делает безопасный и полезный ИИ. Claude — семейство моделей (Opus — самая мощная, Sonnet — быстрая, Haiku — лёгкая; плюс Fable). Этот бот работает на Claude Code. Подробнее: anthropic.com, claude.com.","⬅️ К «Про этот бот»",None),
 "md_chat":("💬 Чат","Чат","💬 ЧАТ — обычный диалог: вопросы, тексты, разбор материалов, поиск. Просто напиши, что нужно.","⬅️ К режимам",None),
 "md_code":("💻 Код","Код","💻 КОД — работа с кодом и файлами: писать, править, запускать, деплой. Напиши задачу по коду или пришли файл.","⬅️ К режимам",None),
 "md_design":("🎨 Дизайн","Дизайн","🎨 ДИЗАЙН — интерфейсы, вёрстка, макеты (плагин Frontend-design / Lovable). Опиши, что нарисовать/собрать.","⬅️ К режимам",None),
 "md_cowork":("🧩 Cowork","Cowork","🧩 COWORK — совместная агентная работа над задачей по шагам. Опиши цель — проведу по этапам до результата.","⬅️ К режимам",None),
 "plg_tg":("🔗 Telegram","Telegram","🔗 Telegram — плагин связи бота с этим чатом: приём сообщений, ответы, кнопки, файлы.","⬅️ К плагинам",None),
 "plg_fd":("🎨 Frontend-design","Frontend","🎨 Frontend-design — помощь с дизайном интерфейсов и вёрсткой.","⬅️ К плагинам",None),
 "plg_atl":("🗂 Atlassian","Atlassian","🗂 Atlassian — работа с Jira и Confluence: задачи и документация.","⬅️ К плагинам",None),
 "fn_analyze":("🍪 Разбор","Разбор","🍪 Разбор — полный разбор материала: что это, ключевое по пунктам, выводы. Пришли файл или напиши «разбери».","⬅️ К функциям",None),
 "fn_summary":("✂️ Саммари","Саммари","✂️ Саммари — краткая выжимка сути (5–8 предложений), без воды.","⬅️ К функциям",None),
 "fn_plan":("🗺 План","План","🗺 План — пошаговый план действий: что делать, кто, срок.","⬅️ К функциям",None),
 "fn_priority":("🎯 Приоритеты","Приоритеты","🎯 Приоритеты — топ-3–5 самого важного и срочного с обоснованием.","⬅️ К функциям",None),
 "fn_tasks":("✅ Задачи","Задачи","✅ Задачи — конкретные задачи из материала; можно завести в Task-бот.","⬅️ К функциям",None),
 "fn_risks":("⚠️ Риски","Риски","⚠️ Риски — проблемы, узкие места, на что обратить внимание.","⬅️ К функциям",None),
 "fn_numbers":("📊 Цифры","Цифры","📊 Цифры — итоги по таблицам: суммы, средние, аномалии.","⬅️ К функциям",None),
 "fn_advice":("💡 Советы","Советы","💡 Советы — конкретные рекомендации и следующие шаги.","⬅️ К функциям",None),
 "cn_notion":("📓 Notion","Notion","📓 Notion — база знаний и страницы. Пример: «возьми из Notion страницу X».","⬅️ К коннекторам",None),
 "cn_drive":("📁 Drive","Drive","📁 Google Drive — файлы и документы. Пример: «найди на Диске отчёт».","⬅️ К коннекторам",None),
 "cn_gmail":("✉️ Gmail","Gmail","✉️ Gmail — почта. Пример: «найди письмо от …».","⬅️ К коннекторам",None),
 "cn_cal":("📅 Calendar","Calendar","📅 Google Calendar — события и расписание. Пример: «что в календаре на завтра».","⬅️ К коннекторам",None),
 "cn_asana":("✅ Asana","Asana","✅ Asana — задачи проектов.","⬅️ К коннекторам",None),
}
def t(x): return {"text":x}
base = screen[:-5] if screen.endswith("_info") else screen
if screen in NAV:
    title,rows,back,ph=NAV[screen]
    kb=[[t(x) for x in row] for row in rows]
    if back: kb.append([t(back)])
    text=title
elif base in ITEMS:
    name,short,info,pl,primary=ITEMS[base]
    if screen.endswith("_info"):
        text=info
        kb=[]
        if primary: kb.append([t(primary)])
        kb.append([t(pl),t("☰ Меню")])
    else:
        text=name
        kb=[[t("❓ Что это: "+short)]]
        if primary: kb.append([t(primary)])
        kb.append([t(pl),t("☰ Меню")])
    ph="Выбери…"
else:
    title,rows,back,ph=NAV["home"]; kb=[[t(x) for x in row] for row in rows]; text=title
km={"keyboard":kb,"resize_keyboard":True,"is_persistent":True,"input_field_placeholder":ph}
body={"chat_id":chat_id,"text":text,"reply_markup":json.dumps(km)}
req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req,timeout=30))
print("menu '"+screen+"' sent:", d.get("ok"))
PY
