#!/bin/zsh
# Быстрая смена модели ядра бриджа (@pastila_code_remote_bot) на лету.
#
# Использование:  switch-bridge-model.sh <sonnet|opus|fable>
#
# Как работает: пишем новую модель в .bridge-model, ставим флаг .bridge-switch,
# затем с небольшой задержкой роняем текущий процесс ядра. Цикл в
# start-code-bridge.command увидит флаг и поднимет ядро уже на новой модели.
#
# ВАЖНО: смена модели = новая сессия. Контекст текущего диалога обнуляется —
# это неизбежно, модель фиксируется при запуске процесса.

MODEL="${1:-sonnet}"
case "$MODEL" in
  sonnet|opus|fable) ;;
  *) echo "Неизвестная модель: $MODEL (нужно sonnet|opus|fable)"; exit 1 ;;
esac

echo "$MODEL" > "$HOME/pastila_bot/.bridge-model"
touch "$HOME/pastila_bot/.bridge-switch"

# Даём боту 2 сек, чтобы успеть отправить ответ в Telegram, потом роняем ядро.
# Паттерн специфичен для бриджа — другие claude-процессы не заденет.
( sleep 2; pkill -f "channels plugin:telegram@claude-plugins-official" ) >/dev/null 2>&1 &

echo "OK: ядро переключается на $MODEL, перезапуск через ~2-3 сек."
