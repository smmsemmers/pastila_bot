#!/bin/zsh
# menu.sh <chat_id> <screen>
# Рисует ИНЛАЙН-меню бриджа (кнопки в сообщении, callback_data). Уживается с кнопкой-словом «Меню»
# (setChatMenuButton type=commands) — как у VPN-бота: слева «Меню», в сообщении инлайн-навигация.
# Экраны: home | models | bots | botbridge | bottask | botgpt | id | about | integrations | subs | help
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
def b(text,data=None,url=None):
    d={"text":text}
    if url: d["url"]=url
    else: d["callback_data"]=data
    return d
HOME=[b("☰ Меню","nav_home")]
S={
 "home":{"text":"☰ МЕНЮ — выбери раздел. Слева кнопка «Меню» = все команды, тут кнопки-разделы:",
   "kb":[[b("🍎 Модели","nav_models"),b("🍫 Боты","nav_bots")],
         [b("🍬 ID","nav_id"),b("ℹ️ О боте","nav_about")],
         [b("🔌 Интеграции","nav_integr"),b("🧁 Велком","nav_welcome")],
         [b("❓ Помощь","nav_help"),b("🍭 Подписки","nav_subs")]]},
 "models":{"text":("🍎 ДОСТУПНЫЕ МОДЕЛИ. Сейчас: 🍎 Opus 4.8.\n\n"
     "🍏 Sonnet 4.6 — быстрая и лёгкая: повседневное, короткие тексты, простые правки.\n"
     "🍎 Opus 4.8 — самая умная: анализ, код, большие разборы, стратегия.\n"
     "🍓 Fable 5 — тексты и креатив: живые тексты, посты.\n\n"
     "Коротко: быстро → Sonnet · сложно → Opus · красиво → Fable. Жми, чтобы переключить:"),
   "kb":[[b("🍏 Sonnet 4.6","set_sonnet"),b("🍎 Opus 4.8","set_opus")],
         [b("🍓 Fable 5","set_fable")], HOME]},
 "bots":{"text":"🍫 БОЛЬШЕ БОТОВ. Нажми бота — описание + кнопка «добавить в группу»:",
   "kb":[[b("🍎 Бридж (Claude)","bot_bridge"),b("🗂 Task-бот","bot_task")],
         [b("🤖 GPT-помощник","bot_gpt")], HOME]},
 "botbridge":{"text":("🍎 БРИДЖ (Claude Code) — @pastila_code_remote_bot\n\nГлавный ассистент канала: "
     "разбор файлов и экспортов Claude, Notion/Drive/Gmail/Calendar, картинки, тексты, задачи, смена модели."),
   "kb":[[b("➕ Добавить в группу",url="https://t.me/pastila_code_remote_bot?startgroup=true")],
         [b("⬅️ Боты","nav_bots"),b("☰ Меню","nav_home")]]},
 "bottask":{"text":("🗂 TASK-БОТ — @PastilaTaskBot\n\nЗадачи и напоминания: /new, /list, /digest, /alerts. "
     "Держит общую таблицу команды, шлёт расписания."),
   "kb":[[b("➕ Добавить в группу",url="https://t.me/PastilaTaskBot?startgroup=true")],
         [b("⬅️ Боты","nav_bots"),b("☰ Меню","nav_home")]]},
 "botgpt":{"text":("🤖 GPT-ПОМОЩНИК — @pastila_gPT_remote_bot\n\nСвободный чат с GPT: напиши вопрос. /start, /help."),
   "kb":[[b("➕ Добавить в группу",url="https://t.me/pastila_gPT_remote_bot?startgroup=true")],
         [b("⬅️ Боты","nav_bots"),b("☰ Меню","nav_home")]]},
 "id":{"text":("🍬 ID. Этот чат: "+str(chat_id)+"\n\nКоманда /id показывает id чата и твой user_id "
     "(в группе — id группы). Нужно, чтобы добавить новую группу в доступ бота."),"kb":[HOME]},
 "about":{"text":("ℹ️ О БОТЕ. Главный ассистент канала на базе Claude Code: разбор файлов и экспортов Claude, "
     "Notion/Drive/Gmail/Calendar, картинки, тексты, смена модели.\n\nВладелец — Лена "
     "(в прошлом @elenaisanewleet, сейчас @sorrbouthat). По вопросам и доступам — к ней."),"kb":[HOME]},
 "integrations":{"text":("🔌 ИНТЕГРАЦИИ (что подключено и зачем):\n\n"
     "• Notion — база знаний/страницы: «возьми из Notion …».\n"
     "• Google Drive — файлы/документы: «найди на Диске …».\n"
     "• Gmail — почта: «найди письмо …».\n"
     "• Google Calendar — события: «что в календаре …».\n"
     "• Asana — задачи проектов.\n\n"
     "Просто напиши задачу словами — сам обращусь к нужной интеграции. "
     "Новые коннекторы подключаются в настройках claude.ai (из Telegram создать нельзя)."),"kb":[HOME]},
 "subs":{"text":("🍭 ПОДПИСКИ — в разработке. Планируются тарифы по токенам в день: анлимит · средний · меньший. "
     "Пока пользование свободное."),"kb":[HOME]},
 "help":{"text":("❓ ПОМОЩЬ. Напиши задачу или пришли файл (PDF, Excel, JSON, архив). Кнопка «Меню» слева — "
     "все команды. Разделы — кнопками в этом меню."),"kb":[HOME]},
}
s=S.get(screen,S["home"])
body={"chat_id":chat_id,"text":s["text"],"reply_markup":json.dumps({"inline_keyboard":s["kb"]})}
req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req,timeout=30))
print("inline menu '"+screen+"' sent:", d.get("ok"))
PY
