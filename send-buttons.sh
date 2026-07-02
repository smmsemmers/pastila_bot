#!/bin/zsh
# Отправить инлайн-кнопки в Telegram от имени бриджа @pastila_code_remote_bot.
# Использование:
#   send-buttons.sh <chat_id> "<текст>" "Label1=data1;Label2=data2;..."
# Кнопки идут по 2 в ряд. Когда пользователь нажмёт — плагин пришлёт агенту
# сообщение "[button] <data>", и бридж выполняет соответствующее действие.

CHAT="$1"; TEXT="$2"; BTNS="$3"
TOKEN=$(grep -oE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' "$HOME/.claude/channels/telegram/.env" | head -1)
[ -z "$TOKEN" ] && { echo "нет токена бриджа"; exit 1; }

KB=$(python3 - "$BTNS" << 'PY'
import sys, json
btns = [b for b in sys.argv[1].split(";") if b.strip()]
rows, row = [], []
for b in btns:
    label, _, data = b.partition("=")
    row.append({"text": label.strip(), "callback_data": (data or label).strip()[:60]})
    if len(row) == 2:
        rows.append(row); row = []
if row:
    rows.append(row)
print(json.dumps({"inline_keyboard": rows}, ensure_ascii=False))
PY
)
curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${TEXT}" \
  --data-urlencode "reply_markup=${KB}" >/dev/null && echo "sent"
