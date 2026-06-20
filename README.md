# 🍬 Pastila OS — Task Bot

Бот для заведения задач через диалог с кнопками. Постит готовый таск в нужный топик группы и пишет строку в Google-таблицу.

## Что делает

1. Команда `/new` запускает диалог
2. Бот по шагам спрашивает: название → DoD → кто → дедлайн → шаги → материалы → теги → статус
3. Собирает красивый таск по шаблону Pastila OS
4. Постит в топик: Лена → Tasks—Лена, Глеб → Tasks—Глеб, Лена+Глеб → оба
5. Тегает исполнителя (@elenaisanewleet / @foxruso)
6. Пишет строку в Google Sheets

---

## Перед деплоем нужно собрать 8 переменных

### 1. BOT_TOKEN
Токен твоего бота от @BotFather (тот, что Reminder / @neaizmirsti_bot).

### 2-4. GROUP_CHAT_ID, THREAD_LENA, THREAD_GLEB

Это id группы и id топиков. Добываются так:

**Способ через @getidsbot или @RawDataBot:**
- Добавь @RawDataBot в группу на минуту
- Напиши любое сообщение в топик Tasks — Лена
- Бот покажет JSON. Найди в нём:
  - `chat.id` → это GROUP_CHAT_ID (отрицательное, типа `-1001234567890`)
  - `message_thread_id` → это THREAD_LENA для этого топика
- Повтори в топике Tasks — Глеб → получишь THREAD_GLEB
- Удали @RawDataBot

**Альтернатива — из ссылки на топик:**
- Открой топик в веб-версии Telegram, скопируй ссылку вида
  `https://t.me/c/2493586/12` — последнее число `12` это thread_id,
  а `2493586` → GROUP_CHAT_ID будет `-100` + это число = `-1002493586`

### 5. SHEET_ID
Из адреса таблицы между `/d/` и `/edit`:
`docs.google.com/spreadsheets/d/`**`ВОТ_ЭТОТ_КУСОК`**`/edit`
У тебя: `1Mlwvuw4bc7ove-2PS20fYybgWlN0rXQnkrfSR7lkVRM`

### 6. SHEET_NAME
Имя листа. У тебя `Sheet1`.

### 7. GOOGLE_CREDENTIALS — сервисный ключ
Это JSON-ключ сервисного аккаунта Google. Получить:
1. console.cloud.google.com → создай проект (или возьми существующий)
2. APIs & Services → Enable APIs → включи **Google Sheets API** и **Google Drive API**
3. Credentials → Create Credentials → **Service Account**
4. Создай аккаунт, зайди в него → вкладка Keys → Add Key → JSON → скачается файл
5. Открой этот JSON, скопируй **всё содержимое одной строкой** → это значение GOOGLE_CREDENTIALS
6. **ВАЖНО:** в JSON есть поле `client_email` (типа `xxx@xxx.iam.gserviceaccount.com`).
   Открой свою Google-таблицу → Настройки доступа → добавь этот email как **Редактор**.
   Иначе бот не сможет писать в таблицу.

### 8. TAG_LENA / TAG_GLEB
Уже прописаны по умолчанию: `@elenaisanewleet` и `@foxruso`.

---

## Деплой на Render

1. Залей этот проект на GitHub (новый репозиторий)
2. Render → New → **Background Worker** (НЕ Web Service! у бота нет порта)
3. Подключи репозиторий
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `python bot.py`
6. В разделе **Environment** добавь все 8 переменных выше
   (GOOGLE_CREDENTIALS — весь JSON одной строкой)
7. Deploy

Через минуту бот оживёт. Напиши ему `/new` в личку или в группе.

---

## Важные нюансы

- **Privacy mode** бота должен быть выключен (мы это уже сделали в @BotFather).
- Бот должен быть **админом** группы с правом отправки сообщений.
- На бесплатном плане Render воркер может «засыпать» при простое — для постоянной работы держи активность или возьми платный план ($7/мес). Но для редких задач бесплатного хватает.
- Если бот пишет таск, но не пишет в таблицу — почти всегда не выдан доступ сервисному email к таблице (пункт 7, шаг 6).

---

## Локальный запуск (для теста перед деплоем)

```bash
pip install -r requirements.txt
export BOT_TOKEN="..."
export GROUP_CHAT_ID="-100..."
export THREAD_LENA="..."
export THREAD_GLEB="..."
export SHEET_ID="..."
export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
python bot.py
```
