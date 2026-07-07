#!/bin/zsh
# Самоперезапуск бриджа @pastila_code_remote_bot на новую схему (цикл + пикер).
# Запускать ТОЛЬКО отсоединённо (nohup ... &), т.к. на шаге pkill умрёт текущее ядро.
#
#   1) ждём пару секунд, чтобы бридж успел отправить ответ в Telegram;
#   2) роняем текущий процесс ядра (старый лаунчер без цикла);
#   3) даём Telegram отпустить getUpdates;
#   4) открываем новый лаунчер в Терминале (двойной клик программно) — он поднимет
#      ядро уже с пикером модели и циклом авто-перезапуска.

sleep 2
pkill -f "channels plugin:telegram@claude-plugins-official"
sleep 4
open "$HOME/pastila_bot/start-code-bridge.command"
