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
echo "   1) Sonnet 5   — быстрый и дешёвый"
echo "   2) Opus 5     — максимум качества (по умолчанию)"
echo "   3) Fable 5    — для самых сложных вызовов"
echo "   4) Haiku 4.5  — самый быстрый"
printf "Выбор [1/2/3/4, Enter = Opus 5, 5 сек]: "
read -t 5 choice
case "$choice" in
  1) echo claude-sonnet-5 > "$MODEL_FILE" ;;
  3) echo claude-fable-5  > "$MODEL_FILE" ;;
  4) echo claude-haiku-4-5 > "$MODEL_FILE" ;;
  *) echo claude-opus-5   > "$MODEL_FILE" ;;
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
