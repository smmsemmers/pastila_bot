# Как запустить бот локально

## Требования

- Python 3.12 (`python3.12 --version`)
- Токены в `.env` файле (или напрямую в переменных окружения)

## Быстрый старт

```bash
# 1. Перейти в папку проекта
cd ~/pastila_bot

# 2. Создать виртуальное окружение (один раз)
python3.12 -m venv venv

# 3. Активировать
source venv/bin/activate

# 4. Установить зависимости (один раз или после изменений в requirements.txt)
pip install -r requirements.txt

# 5. Создать .env с токенами (один раз — скопировать и заполнить)
cp .env.example .env   # если нет — создать вручную

# 6. Запустить
python bot.py
```

## Переменные окружения (.env)

```
BOT_TOKEN=...                  # токен @PastilaTaskBot (BotFather)
GROUP_CHAT_ID=...              # ID группы (число, обычно отрицательное)
THREAD_LENA=...                # ID топика Лены
THREAD_GLEB=...                # ID топика Глеба
SHEET_ID=1Mlwvuw4bc7ove-2PS20fYybgWlN0rXQnkrfSR7lkVRM
SHEET_NAME=Sheet1
GOOGLE_CREDENTIALS=...         # JSON сервисного аккаунта (одной строкой)
OPENAI_API_KEY=...             # для Whisper (голос)
OPENROUTER_API_KEY=...         # для всех текстовых LLM
TAG_LENA=@elenaisanewleet
TAG_GLEB=@foxruso
TZ=Europe/Moscow
```

## Остановить

`Ctrl+C` в терминале.

## Деплой на Render

Render подхватывает изменения автоматически при `git push origin main`.  
Переменные окружения задаются в дашборде Render → Environment.

Проверить логи: Render Dashboard → pastila-task-bot → Logs.

## Обновить зависимости

```bash
pip install -r requirements.txt
```

## Частые проблемы

| Ошибка | Решение |
|--------|---------|
| `python3.12: command not found` | Установить Python 3.12: `brew install python@3.12` |
| `BOT_TOKEN не задан` | Проверить `.env` или переменные окружения |
| `gspread.exceptions.APIError` | Проверить GOOGLE_CREDENTIALS и что сервисный аккаунт добавлен в таблицу |
| `httpx.ConnectError` | Нет интернета или заблокирован OpenRouter/OpenAI |
