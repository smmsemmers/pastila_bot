#!/bin/zsh
# menu.sh <chat_id> <screen>
# Нижняя сетка (reply-клавиатура) бриджа с навигацией и «назад до последнего».
# Экраны: home | models | bots | botbridge | bottask | botgpt | about | plugins | functions | connectors
# Порядок разделов задан Леной. Токен из ~/.claude/channels/telegram/.env (не печатается).

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
def t(x): return {"text":x}
BACK=[t("⬅️ Назад в меню")]
S={
 "home":{"text":"☰ МЕНЮ — выбери раздел (навигация в сетке снизу, ⬅️ Назад вернёт сюда):",
   "kb":[[t("🍎 Доступные модели"),t("🍫 Больше ботов")],
         [t("ℹ️ Про этот бот"),t("🔌 Доступные плагины")],
         [t("⚙️ Функции"),t("🔗 Коннекторы")]],
   "ph":"☰ Меню — выбери раздел…"},
 "models":{"text":("🍎 ДОСТУПНЫЕ МОДЕЛИ. Сейчас: 🍎 Opus 4.8.\n\n"
     "🍏 Sonnet 4.6 — быстрая и лёгкая: повседневное, короткие тексты, простые правки.\n"
     "🍎 Opus 4.8 — самая умная: анализ, код, большие разборы, стратегия.\n"
     "🍓 Fable 5 — тексты и креатив: живые тексты, посты.\n\n"
     "Коротко: быстро → Sonnet · сложно → Opus · красиво → Fable. Жми, чтобы переключить:"),
   "kb":[[t("🍏 Sonnet 4.6"),t("🍎 Opus 4.8")],[t("🍓 Fable 5")], BACK],"ph":"Выбери модель…"},
 "bots":{"text":"🍫 БОЛЬШЕ БОТОВ. Нажми бота — описание + добавить в группу:",
   "kb":[[t("🍎 Бридж (Claude)"),t("🗂 Task-бот")],[t("🤖 GPT-помощник")], BACK],"ph":"Выбери бота…"},
 "botbridge":{"text":("🍎 БРИДЖ (Claude Code) — @pastila_code_remote_bot\n\nГлавный ассистент канала: разбор "
     "файлов и экспортов Claude, Notion/Drive/Gmail/Calendar, картинки, тексты, задачи, смена модели."),
   "kb":[[t("➕ Добавить бридж в группу")],[t("⬅️ К ботам"),t("☰ Меню")]],"ph":"⬅️ Назад"},
 "bottask":{"text":("🗂 TASK-БОТ — @PastilaTaskBot\n\nЗадачи и напоминания: /new, /list, /digest, /alerts. "
     "Держит общую таблицу команды, шлёт расписания."),
   "kb":[[t("➕ Добавить Task в группу")],[t("⬅️ К ботам"),t("☰ Меню")]],"ph":"⬅️ Назад"},
 "botgpt":{"text":("🤖 GPT-ПОМОЩНИК — @pastila_gPT_remote_bot\n\nСвободный чат с GPT: напиши вопрос. /start, /help."),
   "kb":[[t("➕ Добавить GPT в группу")],[t("⬅️ К ботам"),t("☰ Меню")]],"ph":"⬅️ Назад"},
 "about":{"text":("ℹ️ ПРО ЭТОТ БОТ. Главный ассистент канала на базе Claude Code: разбор файлов и экспортов "
     "Claude, Notion/Drive/Gmail/Calendar, картинки, тексты, смена модели ядра.\n\n"
     "Владелец — Лена (в прошлом @elenaisanewleet, сейчас @sorrbouthat). По вопросам и доступам — к ней.\n\n"
     "id этого чата: "+str(chat_id)+" (команда /id)."),"kb":[BACK],"ph":"⬅️ Назад в меню"},
 "plugins":{"text":("🔌 ДОСТУПНЫЕ ПЛАГИНЫ. Плагины — расширения бота. Сейчас активны:\n"
     "• Telegram — связь с этим чатом.\n• Frontend-design — помощь с дизайном интерфейсов.\n"
     "• Atlassian — Jira/Confluence.\n\nНовые плагины подключаются в настройках Claude Code."),
   "kb":[BACK],"ph":"⬅️ Назад в меню"},
 "functions":{"text":("⚙️ ФУНКЦИИ — что я умею:\n• разбор файлов (PDF, Excel, JSON, архивы) и экспортов Claude;\n"
     "• саммари, план, стратегия, приоритеты, задачи, риски, цифры, советы;\n• поиск, тексты и правки;\n"
     "• работа с картинками;\n• задачи в общую таблицу (подхватит Task-бот);\n• смена модели ядра.\n\n"
     "Просто напиши, что нужно, или пришли файл."),"kb":[BACK],"ph":"⬅️ Назад в меню"},
 "connectors":{"text":("🔗 КОННЕКТОРЫ — подключённые источники:\n• Notion — база знаний/страницы.\n"
     "• Google Drive — файлы и документы.\n• Gmail — почта.\n• Google Calendar — события.\n• Asana — задачи.\n\n"
     "Пиши словами («возьми из Notion…», «найди письмо…») — сам обращусь к нужному. "
     "Создать новый коннектор из Telegram нельзя — это в настройках claude.ai."),"kb":[BACK],"ph":"⬅️ Назад в меню"},
}
s=S.get(screen,S["home"])
kb={"keyboard":s["kb"],"resize_keyboard":True,"is_persistent":True,"input_field_placeholder":s["ph"]}
body={"chat_id":chat_id,"text":s["text"],"reply_markup":json.dumps(kb)}
req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req,timeout=30))
print("menu '"+screen+"' sent:", d.get("ok"))
PY
