#!/bin/zsh
# Мост Claude Code ↔ Telegram — бот @pastila_code_remote_bot.
#
# Как пользоваться:
#   • Двойной клик по этому файлу в Finder — откроется Терминал и поднимет мост.
#   • При старте спросит, какую модель ядра поднять (Sonnet / Opus / Fable).
#     По умолчанию (Enter или 5 сек тишины) — Sonnet.
#   • Пока это окно открыто — бот отвечает в Telegram. Закрыл окно — мост отключился.
#   • Модель можно сменить прямо из Telegram (/sonnet /opus /fable или «модель опус»):
#     бридж уронит текущий процесс, а этот цикл поднимет ядро уже на новой модели.
#   • caffeinate не даёт Маку уснуть, пока мост работает.
#
# Запускать только В ОДНОМ окне (один процесс на бота), иначе конфликт токена.

cd "$HOME/pastila_bot" || exit 1

MODEL_FILE="$HOME/pastila_bot/.bridge-model"
SWITCH_FLAG="$HOME/pastila_bot/.bridge-switch"

# ── Пикер модели при старте ─────────────────────────────────────────
echo "🚀 Мост @pastila_code_remote_bot — выбор модели ядра:"
echo "   1) Sonnet 4.6  — быстрый и дешёвый (по умолчанию)"
echo "   2) Opus 4.8    — максимум качества"
echo "   3) Fable 5     — экспериментальная"
printf "Выбор [1/2/3, Enter = Sonnet, 5 сек]: "
read -t 5 choice
case "$choice" in
  2) echo opus   > "$MODEL_FILE" ;;
  3) echo fable  > "$MODEL_FILE" ;;
  *) echo sonnet > "$MODEL_FILE" ;;
esac
echo ""

# ── Цикл: поднимаем ядро; если была смена модели — перезапускаем ────
while true; do
  MODEL="$(cat "$MODEL_FILE" 2>/dev/null)"
  [[ -z "$MODEL" ]] && MODEL=sonnet
  echo "▶️  Поднимаю ядро: $MODEL  (закрой окно, чтобы остановить совсем)"
  # --permission-mode auto: бридж действует без переспросов на обычных операциях
  # (чтение/разбор файлов), но с защитой от опасного.
  caffeinate -dimsu claude --permission-mode auto --model "$MODEL" \
    --channels plugin:telegram@claude-plugins-official

  # Ядро завершилось. Флаг .bridge-switch → это была смена модели, перезапускаем.
  if [[ -f "$SWITCH_FLAG" ]]; then
    rm -f "$SWITCH_FLAG"
    echo "🔄 Смена модели → перезапуск ядра…"
    sleep 1
    continue
  fi
  echo "🛑 Мост остановлен."
  break
done
