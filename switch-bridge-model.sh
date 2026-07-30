#!/bin/zsh
# Смена модели ядра бриджа (@pastila_code_remote_bot) на лету.
#
# Использование:
#   switch-bridge-model.sh <модель>
#   где <модель> — алиас (opus|sonnet|fable|haiku) или полное имя (claude-opus-5 и т.п.)
#
# Как работает: пишем модель в .bridge-model, ставим флаг .bridge-switch,
# роняем текущее ядро; цикл в start-code-bridge.command поднимает ядро на новой модели.
# ВАЖНО: смена модели = НОВАЯ сессия, контекст диалога обнуляется.

ARG="${1:-opus}"

# Алиасы → полные имена (актуальные поколения). ВЕРИФИЦИРОВАНО, что ядро их принимает.
case "$ARG" in
  opus|Opus)     MODEL=claude-opus-5 ;;
  opus4.8)       MODEL=claude-opus-4-8 ;;
  opus4.7)       MODEL=claude-opus-4-7 ;;
  opus4.6)       MODEL=claude-opus-4-6 ;;
  sonnet|Sonnet) MODEL=claude-sonnet-5 ;;
  sonnet4.6)     MODEL=claude-sonnet-4-6 ;;
  fable|Fable)   MODEL=claude-fable-5 ;;
  haiku|Haiku)   MODEL=claude-haiku-4-5 ;;
  *)             MODEL="$ARG" ;;   # полное имя — как есть
esac

# Белый список проверенных моделей (защита от опечатки, иначе ядро не поднимется).
VALID=(claude-opus-5 claude-opus-4-8 claude-opus-4-7 claude-opus-4-6 \
       claude-sonnet-5 claude-sonnet-4-6 claude-fable-5 claude-haiku-4-5)
ok=0
for v in "${VALID[@]}"; do [[ "$MODEL" == "$v" ]] && ok=1; done
if [[ $ok -ne 1 ]]; then
  echo "❌ Модель '$MODEL' не в списке проверенных. Доступны: ${VALID[*]}"
  exit 1
fi

echo "$MODEL" > "$HOME/pastila_bot/.bridge-model"
touch "$HOME/pastila_bot/.bridge-switch"
( sleep 2; pkill -f "channels plugin:telegram@claude-plugins-official" ) >/dev/null 2>&1 &
echo "OK: ядро переключается на $MODEL, перезапуск через ~2-3 сек (новая сессия)."
