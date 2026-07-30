#!/bin/zsh
# menu.sh <chat_id> <screen>
# Рисует экран нижней сетки (reply-клавиатуры) бриджа. Экраны:
#   home | models | bots | id | about | subs | help
# Токен берётся из ~/.claude/channels/telegram/.env (не печатается).
# Навигация: кнопки шлют текст, бридж на него зовёт menu.sh с нужным экраном.

CHAT_ID="$1"
SCREEN="${2:-home}"

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

BACK=[{"text":"⬅️ Назад в меню"}]
screens={
 "home":{
   "text":"☰ МЕНЮ — выбери раздел (сетка снизу меняется по разделам):",
   "kb":[[{"text":"🍎 Модели"},{"text":"🍫 Боты"}],
         [{"text":"🍬 ID"},{"text":"ℹ️ О боте"}],
         [{"text":"🧁 Велком"},{"text":"❓ Помощь"}],
         [{"text":"🍭 Подписки"}]],
   "ph":"☰ Меню — выбери раздел…"},
 "models":{
   "text":("🍎 ДОСТУПНЫЕ МОДЕЛИ. Сейчас активна: 🍎 Opus 4.8.\n\n"
     "🍏 Sonnet 4.6 — быстрая и лёгкая: повседневные вопросы, короткие тексты, простые правки.\n"
     "🍎 Opus 4.8 — самая умная: глубокий анализ, код, большие разборы, стратегия.\n"
     "🍓 Fable 5 — тексты и креатив: живые, художественные тексты, посты.\n\n"
     "Коротко: быстро → Sonnet · сложно → Opus · красиво → Fable.\n"
     "Жми модель, чтобы переключиться."),
   "kb":[[{"text":"🍏 Sonnet 4.6"},{"text":"🍎 Opus 4.8"}],
         [{"text":"🍓 Fable 5"}], BACK],
   "ph":"Выбери модель…"},
 "bots":{
   "text":"🍫 БОЛЬШЕ БОТОВ. Нажми бота — покажу описание, велком и как добавить его в группу.",
   "kb":[[{"text":"🍎 Бридж (Claude)"},{"text":"🗂 Task-бот"}],
         [{"text":"🤖 GPT-помощник"}], BACK],
   "ph":"Выбери бота…"},
 "id":{
   "text":("🍬 ID. Этот чат: "+str(chat_id)+"\n\n"
     "Команда /id показывает id текущего чата и твой user_id. В группе — id группы, в личке — твой. "
     "Нужно, чтобы добавить новую группу в доступ бота."),
   "kb":[BACK],"ph":"⬅️ Назад в меню"},
 "about":{
   "text":("ℹ️ О БОТЕ. Я — главный ассистент канала на базе Claude Code: разбор файлов и экспортов Claude, "
     "Notion / Google Drive / Gmail / Calendar, картинки, тексты, смена модели ядра.\n\n"
     "Владелец — Лена (в прошлом @elenaisanewleet, сейчас @sorrbouthat). По вопросам и доступам пишите ей."),
   "kb":[BACK],"ph":"⬅️ Назад в меню"},
 "subs":{
   "text":("🍭 ПОДПИСКИ — в разработке. Планируются тарифы по объёму токенов в день: "
     "анлимит · средний · меньший. Пока пользование свободное."),
   "kb":[BACK],"ph":"⬅️ Назад в меню"},
 "help":{
   "text":("❓ ПОМОЩЬ. Просто напиши задачу или пришли файл (PDF, Excel, JSON, архив). "
     "Кнопки снизу — быстрые разделы. Полный список команд — кнопка «Меню» слева (в личке) или «/» в группе."),
   "kb":[BACK],"ph":"⬅️ Назад в меню"},
}
s=screens.get(screen, screens["home"])
kb={"keyboard":s["kb"],"resize_keyboard":True,"is_persistent":True,"input_field_placeholder":s["ph"]}
body={"chat_id":chat_id,"text":s["text"],"reply_markup":json.dumps(kb)}
req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req,timeout=30))
print("menu screen '"+screen+"' sent:", d.get("ok"))
PY
