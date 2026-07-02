#!/bin/zsh
# Мост Claude Code ↔ Telegram — бот @pastila_code_remote_bot.
#
# Как пользоваться:
#   • Двойной клик по этому файлу в Finder — откроется Терминал и поднимет мост.
#   • Пока это окно открыто — бот отвечает в Telegram. Закрыл окно — мост отключился.
#   • caffeinate не даёт Маку уснуть, пока мост работает.
#
# Запускать только В ОДНОМ окне (один процесс на бота), иначе конфликт токена.

cd "$HOME/pastila_bot" || exit 1
echo "🚀 Поднимаю мост @pastila_code_remote_bot… (закрой окно, чтобы остановить)"
# --permission-mode auto: бридж действует без переспросов на обычных операциях
# (чтение/разбор файлов), но с защитой от опасного — чтобы не зависал при работе через Telegram.
caffeinate -dimsu claude --permission-mode auto --channels plugin:telegram@claude-plugins-official
