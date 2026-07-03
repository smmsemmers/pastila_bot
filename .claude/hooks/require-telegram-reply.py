#!/usr/bin/env python3
"""Stop hook: если во входящем был Telegram-месседж, а ответа через reply-инструмент
за этот ход не было — блокируем стоп и напоминаем ответить в чат.

Читает JSON со stdin (session_id, transcript_path, stop_hook_active).
Печатает {"decision":"block","reason":...} чтобы заставить продолжить, либо ничего.
"""
import json
import re
import sys

REPLY_TOOL = "mcp__plugin_telegram_telegram__reply"
TG_MARKER = "plugin:telegram:telegram"
# входящие от этих аккаунтов — боты (дайджесты/алерты/эхо), ответа в чат не требуют
BOT_USERS = {
    "pastilataskbot",
    "pastila_gpt_remote_bot",
    "pastila_code_remote_bot",
}


def load_lines(path):
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def text_of(entry):
    """Всё текстовое содержимое записи как строка (для поиска маркеров)."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    out.append(str(block.get("text", "")))
                elif block.get("type") == "tool_result":
                    c = block.get("content")
                    if isinstance(c, str):
                        out.append(c)
                    elif isinstance(c, list):
                        for cc in c:
                            if isinstance(cc, dict) and cc.get("type") == "text":
                                out.append(str(cc.get("text", "")))
    return "\n".join(out)


def is_tool_result(entry):
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return True
    return False


def used_reply(entry):
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") == REPLY_TOOL:
                return True
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    # защита от бесконечного цикла: если уже блокировали и снова стоп — пропускаем
    if data.get("stop_hook_active"):
        return

    path = data.get("transcript_path")
    if not path:
        return

    entries = load_lines(path)
    if not entries:
        return

    # находим границу текущего хода: последняя пользовательская запись,
    # которая не является tool_result
    boundary = 0
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.get("type") == "user" and not is_tool_result(e):
            boundary = i
            break

    turn = entries[boundary:]

    # был ли во входящем этого хода Telegram-месседж ОТ ЖИВОГО пользователя
    # (не от бота — дайджесты/алерты @PastilaTaskBot и т.п. ответа не требуют)
    def from_real_user(e):
        if e.get("type") != "user":
            return False
        t = text_of(e)
        if TG_MARKER not in t:
            return False
        users = re.findall(r'user="([^"]+)"', t)
        # хотя бы один автор — не бот
        return any(u.strip().lower() not in BOT_USERS for u in users) if users else True

    inbound_tg = any(from_real_user(e) for e in turn)
    if not inbound_tg:
        return  # не из Telegram или только от ботов — ничего не навязываем

    # был ли вызов reply в этом ходе?
    replied = any(e.get("type") == "assistant" and used_reply(e) for e in turn)
    if replied:
        return  # всё хорошо

    print(json.dumps({
        "decision": "block",
        "reason": (
            "Ты не ответила пользователю в Telegram за этот ход. "
            "ГЛАВНОЕ ПРАВИЛО: каждый ответ отправляй через инструмент "
            f"`{REPLY_TOOL}` с chat_id из входящего сообщения. "
            "Отправь ответ в чат сейчас, затем завершай."
        ),
    }))


if __name__ == "__main__":
    main()
