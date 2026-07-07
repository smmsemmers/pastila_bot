# Pastila OS Task Bot — полное техническое описание

> **Последнее обновление:** июнь 2026
> **Репозиторий:** https://github.com/smmsemmers/pastila_bot
> **Деплой:** Render Background Worker · Python 3.12.7

---

## Глобальная цель

Бот — операционный мозг бизнеса по белёвской пастиле.

**Проблема:** задачи, решения и приоритеты тонут в переписке Telegram. Никто не помнит кто что взял, к какому сроку, и что было решено две недели назад.

**Что даёт бот:**
- Каждое поручение фиксируется структурно и не теряется
- Видно кто что делает и в каком статусе прямо сейчас
- Дедлайны напоминают сами — утром в день дедлайна и за день до
- Вся история переписки становится базой знаний для ИИ
- Голосом можно управлять всем: задачи, аналитика, приоритеты, советы по развитию бизнеса

**Команда:**
- **Глеб** (@foxruso) — контент, коммуникации, стратегия, босс
- **Лена** (@elenaisanewleet) — разработка, техника, продукт

---

## Технический стек

```
┌─────────────────────────────────────────────────────┐
│                   RENDER.COM                        │
│              Background Worker (free)               │
│                  Python 3.12.7                      │
│                                                     │
│  bot.py (2122 строки)  +  llm_router.py (589 строк)│
└──────────────┬──────────────────────────────────────┘
               │ long-polling (не webhook)
               │
       ┌───────▼────────┐
       │  TELEGRAM API  │
       │ @createtask    │
       │  pastila_bot   │
       └───────┬────────┘
               │
    ┌──────────┼──────────┐
    │          │          │
    ▼          ▼          ▼
OPENAI     OPENROUTER  GOOGLE
(Whisper)  (30+ LLM)   SHEETS
```

**Библиотеки:**

| Библиотека | Версия | Для чего |
|---|---|---|
| python-telegram-bot | 21.6 | async фреймворк: ConversationHandler, JobQueue, CallbackQuery |
| gspread | 6.1.2 | чтение/запись Google Sheets |
| google-auth | — | сервисный аккаунт Google |
| httpx | — | async HTTP для OpenAI и OpenRouter |
| tzdata | — | таймзоны (Europe/Moscow по умолчанию) |

---

## Переменные окружения

| Переменная | Обязательна | Для чего |
|---|---|---|
| `BOT_TOKEN` | ✅ | токен бота @createtaskpastila_bot |
| `GROUP_CHAT_ID` | ✅ | id Telegram-группы |
| `THREAD_LENA` | нет | id топика Лены |
| `THREAD_GLEB` | нет | id топика Глеба |
| `SHEET_ID` | ✅ | id Google-таблицы |
| `SHEET_NAME` | нет (Sheet1) | имя листа с задачами |
| `GOOGLE_CREDENTIALS` | ✅ | JSON сервисного аккаунта |
| `OPENAI_API_KEY` | нет | только Whisper (распознавание голоса) |
| `OPENROUTER_API_KEY` | нет | все LLM-функции (30+ моделей) |
| `TRIM_LEVEL` | нет (4) | обрезка контекста 0–10 |
| `CONTEXT_MODE` | нет (truncate) | truncate или squeeze |
| `SQUEEZE_MODEL` | нет (sonnet46) | модель для сжатия контекста |
| `DIGEST_HOUR` | нет (12) | час отправки дайджеста |
| `DIGEST_MINUTE` | нет (0) | минута |
| `TZ` | нет (Europe/Moscow) | таймзона |

---

## Хранилище данных

### Google Sheets — лист `Sheet1` (задачи)

```
| Дата       | Кто          | Задача              | Дедлайн | Статус  | Ссылка             |
|------------|--------------|---------------------|---------|---------|---------------------|
| 2026-06-25 | Лена         | Сверстать лендинг   | 25.07   | 🔵 WIP  | https://t.me/c/... |
| 2026-06-25 | Лена + Глеб  | Запуск сайта        | Backlog | 🟡 TODO | https://t.me/c/... |
```

Ссылка в последней колонке — deep-link прямо на сообщение с задачей в Telegram.

### Google Sheets — лист `llm_config` (настройки роутера)

```
A1: {"models": {"-1001234": {"plan": "sonnet46"}}, "trim": {}, "mode": {}}
```

Создаётся автоматически при первом запуске. Переживает редеплой Render — настройки `/model` и `/trim` не сбрасываются.

### Память процесса — `_CHAT_LOG`

```python
_CHAT_LOG = {chat_id: [(имя, текст), ...]}  # последние 2000 сообщений на чат
```

Накапливается пока воркер живёт. Сбрасывается при перезапуске. Восстанавливается через импорт `result.json`.

---

## Диаграмма всего флоу

```
                        ПОЛЬЗОВАТЕЛЬ
                             │
              ┌──────────────┼──────────────────┐
              │              │                  │
           Команда      Кнопка (inline)    Голосовое
              │              │                  │
    ┌─────────┴──────┐       │          ┌───────┴────────┐
    │                │       │          │  Whisper API   │
    │   /new ─── ConversationHandler    └───────┬────────┘
    │   /list ── read_open_tasks()              │ transcript
    │   /status ─ cmd_status()         ┌────────▼───────┐
    │   /plan ── choose_then_run()     │ _gpt_voice_    │
    │   /analyze ─ choose_then_run()   │ route()        │
    │   /menu ── menu_home_keyboard()  └────────┬───────┘
    │   /digest ─ build_and_send_digest()       │
    │   /alerts ─ build_and_send_alerts()  type?│
    │   /model ── llm.cmd_model()              │
    │   /trim ─── llm.cmd_trim()    ┌──────────┼─────────────────┐
    │   /ai ───── cmd_ai_check()    │          │                 │
    └───────────────────────────────┘          │                 │
                 │                          task│ list/plan/      │ query
                 │                             │ analyze/digest  │
                 ▼                             ▼                 ▼
          publish_task()            (те же функции        _gpt_voice_
                 │                   что по команде)       analyze()
                 ▼                                              │
          GOOGLE SHEETS ◄──────────────────────────────────────┘
         ┌──────────────┐
         │ Sheet1       │ задачи
         │ llm_config   │ настройки
         └──────────────┘
```

---

## Полная карта функций

### 1. Создание задачи — `/new`

```
/new
  │
  └─► ConversationHandler (8 состояний)
        TITLE    → текстом
        DOD      → текстом / [⏭ Пропустить]
        WHO      → [Лена] [Глеб] [Лена + Глеб]
        DEADLINE → ДД.ММ / [⏭ Пропустить → Backlog]
        STEPS    → многострочный текст / [⏭ Пропустить]
        MATERIALS→ текст/ссылка / файл / фото / [⏭ Пропустить]
        TAGS     → текст / [⏭ Пропустить]
        STATUS   → [⚪️ NEW][🟡 TODO][🔵 WIP]...
                        │
                        ▼
                  publish_task()
                  ├── send_message → топик Лены / Глеба / оба
                  │   └── + send_document/photo (если был файл)
                  └── append_task_to_sheet()
                      └── [Дата | Кто | Задача | Дедлайн | Статус | Ссылка]
```

**Маршрутизация постинга:**
- `THREAD_LENA` и `THREAD_GLEB` заданы → задача летит в топик исполнителя; «Лена + Глеб» → в оба
- Не заданы → в общую группу, исполнитель виден по тегу в тексте

---

### 2. Статусы задачи

**8 статусов:**

| Символ | Код | Когда |
|---|---|---|
| ⚪️ | NEW | только заведена |
| 🟡 | TODO | взято в план |
| 🔵 | WIP | в работе |
| 🟠 | WAITING | ждём ответа / материалов |
| 🟣 | REVIEW | на проверке |
| 🟢 | DONE | готово — скрыто из /list |
| 🔴 | BLOCKED | заблокировано |
| ⚫️ | CANCELLED | отменено — скрыто из /list |

**Быстрые кнопки** под каждой задачей (5 кнопок в один тап):
```
[🔵 В работу] [🟠 Ждём] [🟣 Ревью]
[🔴 Блок]     [✅ Done]
```
→ `on_quick_status()` → обновляет сообщение + строку в Sheets одновременно

**Через `/status`** (reply на задачу) → все 8 статусов кнопками → `on_set_status()` → то же самое

---

### 3. Список задач — `/list`

```
/list → read_open_tasks()
          └── Sheets.get_all_records()
              └── фильтр: статус не содержит DONE/CANCELLED
                  └── группировка по "Кто"
                      └── сортировка: Лена → Глеб → Лена+Глеб → прочие
                          └── вывод с дедлайном каждой задачи
```

---

### 4. Напоминания — автоматические

**Дайджест дедлайнов** (`send_digest`) — каждый день в 12:00 МСК:
```
JobQueue.run_daily(12:00)
  └── read_due_today() → задачи где Дедлайн == сегодня (ДД.ММ)
      ├── если есть → send_message в General с тегами @elenaisanewleet и @foxruso
      └── если нет → молчим (не спамим)
```

**Алерты завтрашних дедлайнов** (`send_deadline_alerts`) — тоже в 12:00:
```
JobQueue.run_daily(12:00)
  └── read_tasks_due(завтра)
      └── _alert_targets(who) → маппинг исполнителя на топик
          └── send_message в топик исполнителя с тегом
```

`/digest` и `/alerts` — ручной запуск тех же функций в любой момент.

---

### 5. Голосовой ввод — полный флоу

```
Голосовое сообщение
  │
  ▼
_whisper_transcribe()          ← OpenAI Whisper API
  │ raw text транскрипт
  ▼
_gpt_voice_route(transcript)   ← дешёвая LLM (voice_route → GPT-5.4 nano)
  │ VOICE_ROUTER_PROMPT
  │ Возвращает один из 6 типов:
  │
  ├─ type: "task"    → _draft_from_parsed()
  │                    черновик + кнопки статуса → publish_task()
  │
  ├─ type: "list"    → read_open_tasks()
  │                    + фильтр по who если указан
  │
  ├─ type: "plan"    → llm.prepare_context()
  │                    → llm.resolve_model() → авто или выбранная
  │                    → _gpt_plan() / _gpt_plan_person()
  │
  ├─ type: "analyze" → llm.prepare_context()
  │                    → _gpt_analyze() → до 5 черновиков задач
  │
  ├─ type: "digest"  → build_and_send_digest()
  │
  └─ type: "query"   → _gpt_voice_analyze()
                        VOICE_ANALYSIS_PROMPT (бизнес-аналитик)
                        переписка + контекст команды → развёрнутый ответ
```

**Примеры роутинга:**

| Что говоришь | type | Что происходит |
|---|---|---|
| «поставь задачу на Лену — сверстать лендинг к 25 июля» | task | черновик → выбор статуса → публикация |
| «покажи задачи Глеба» / «что в работе» | list | читает Sheets, показывает список |
| «составь план для Лены» / «что нам делать на этой неделе» | plan | LLM по переписке |
| «найди задачи из переписки» | analyze | LLM → черновики задач |
| «какие дедлайны сегодня» | digest | запускает дайджест |
| «Глеб прав в этом споре?» | query | аналитик с честным мнением |
| «что важнее всего сейчас» | query | приоритеты с обоснованием |
| «куда развивать бизнес» | query | стратегический совет |
| «найди где мы обсуждали партнёрство» | query | поиск по переписке с цитатами |

---

### 6. Анализ переписки — `/analyze`

```
/analyze
  │
  ▼
llm.choose_then_run("analyze")    ← показывает рекомендованную модель
  │                                  + кнопку ▶️ Запустить
  ▼ (после нажатия ▶️)
_run_analyze()
  ├── llm.prepare_context(buf, chat_id, task="analyze")
  │     ├── truncate: trim_chatlog(level 0-10)
  │     └── squeeze: call_llm(SQUEEZE_MODEL) → сжатая выжимка по фактам
  │
  ├── llm.resolve_model() → model_id (авто или выбранная)
  │
  └── _gpt_analyze(transcript, model_id)
        └── до 5 черновиков задач
            └── каждый черновик → кнопки статуса → publish_task()
```

---

### 7. Планирование — `/plan`

```
/plan [необязательный контекст]
  │
  ▼
llm.choose_then_run("plan")
  │
  ▼
_run_plan()
  ├── llm.prepare_context(task="plan")
  ├── llm.resolve_model() → model_id
  └── _gpt_plan(transcript, extra, model_id)
        └── план отдельно для каждого с конкретными пунктами
```

---

### 8. Меню — `/menu`

```
/menu → интерактивное меню
  ├── 🎯 Приоритетная задача
  │     └── выбор человека → read_open_tasks() → топ по ближайшему дедлайну
  │
  ├── 🗂 Напомнить план
  │     └── выбор человека → _gpt_plan_person() с авторезолвом модели
  │
  ├── 📋 Статусы задач
  │     └── выбор человека → _format_status_for() → сгруппированный список
  │
  ├── ➕ Новая задача → запускает /new диалог
  └── 🆔 ID этого чата → chat_id + thread_id (для настройки)
```

---

### 9. Импорт истории переписки

```
Telegram Desktop → Export → Machine-readable JSON → result.json
  │
  ▼ (пришли файл боту)
on_history_import()
  ├── скачивает через Telegram Bot API
  ├── парсит через _parse_export()
  └── prepends к _CHAT_LOG[chat_id][-2000:]
      → /plan, /analyze, голосовой поиск видят прошлую переписку
```

---

## LLM-роутер (llm_router.py)

### Реестр моделей — 24 штуки в 4 тирах

**💎 Флагманы** (сложные задачи, стратегия):
Claude Opus 4.8, GPT-5.5, GPT-5.5 Pro, Gemini 3.1 Pro, Grok 4.3, Fable 5

**⚖️ Balanced** (оптимум цена/качество):
Sonnet 4.6, GPT-5.4, o3, Gemini 3.5 Flash, Qwen 3.7, GLM 5.2, Kimi K2.6, Mistral Large, DeepSeek V4 Pro

**💸 Дешёвые** (быстро и недорого):
Haiku 4.5, GPT-5.4 mini, GPT-4o mini, GPT-5.4 nano, Gemini 3.1 Flash-Lite, Llama 4 Maverick, DeepSeek V4 Flash, Qwen 3.5 Flash

**🆓 Бесплатные**:
GPT-OSS 120B, Qwen3 Coder, Qwen3-Next 80B

---

### Авто-режим (без вызова LLM — чистая эвристика)

```python
score = 0
if len(context) > 6000:  score += 2   # большой контекст
elif len(context) > 2500: score += 1
if task == "plan":        score += 1   # планирование сложнее
if any(hard_marker in text):           # «стратег», «архитектур», «сравни»...
                          score += 1

score >= 3  →  sonnet46     # дорого, но умно
score >= 1  →  gpt4omini   # дёшево, быстро
score == 0  →  dsv4flash   # ультрадёшево
```

---

### Контекст переписки — два режима

```
prepare_context(chat_log, chat_id, task)
  │
  ├── TRUNCATE mode (по умолчанию, всегда для голоса):
  │     trim_chatlog(level 0-10)
  │       level 0:  всё целиком (~2000 сообщений)
  │       level 4:  убираем филлер (ок/ага/👍), последние ~100 реплик
  │       level 10: только суть, последние 6 реплик по 110 символов
  │
  └── SQUEEZE mode (для /plan и /analyze):
        → trim_chatlog(pre-cut) → сначала грубая обрезка
        → call_llm(SQUEEZE_MODEL, сжать_в_факты)
          ДОСЛОВНО сохраняет: цифры, даты, имена, @-теги, задачи
          → компактная выжимка вместо сырого лога
        Откат к truncate если LLM недоступен
```

---

### Персистентность настроек

```
Старт бота:
  _load_llm_state()
    └── _llm_config_ws().acell("A1") → JSON → llm.import_state()

При изменении модели или trim:
  _persist_cb()
    └── asyncio.to_thread(_save_llm_state())
        └── _llm_config_ws().update_acell("A1", json.dumps(export_state()))
```

Настройки `/model` и `/trim` переживают перезапуск бота и редеплой на Render.

---

## Все функции в коде

### bot.py

| Функция | Строка | Что делает |
|---|---|---|
| `get_worksheet()` | 96 | подключение к Google Sheets |
| `_llm_config_ws()` | 112 | вкладка с настройками LLM |
| `_load_llm_state()` | 129 | загрузка настроек из Sheets при старте |
| `_save_llm_state()` | 138 | сохранение настроек в Sheets |
| `append_task_to_sheet()` | 146 | запись новой задачи в таблицу |
| `read_open_tasks()` | 165 | чтение открытых задач из таблицы |
| `update_task_status()` | 190 | обновление статуса задачи в таблице |
| `read_tasks_due()` | 212 | задачи с конкретным дедлайном |
| `read_due_today()` | 236 | задачи на сегодня/завтра |
| `who_keyboard()` | 285 | кнопки выбора исполнителя |
| `status_keyboard()` | 290 | кнопки выбора статуса |
| `build_task_text()` | 328 | форматирование карточки задачи |
| `parse_deadline()` | 374 | парсинг дедлайна (ДД.ММ → нормализация) |
| `message_link()` | 392 | deep-link на сообщение в Telegram |
| `cmd_new()` | 405 | старт диалога создания задачи |
| `get_title()` ... `get_tags()` | 424–564 | шаги ConversationHandler |
| `publish_task()` | 564 | публикация задачи в топик + Sheets |
| `get_status_and_publish()` | 644 | финальный шаг диалога |
| `cmd_list()` | 675 | список открытых задач |
| `cmd_status()` | 707 | смена статуса через reply |
| `on_set_status()` | 734 | обработка выбора статуса |
| `on_quick_status()` | 790 | быстрые кнопки под задачей |
| `build_and_send_digest()` | 832 | дайджест дедлайнов |
| `build_and_send_alerts()` | 882 | алерты на завтра |
| `_whisper_transcribe()` | 987 | распознавание голоса через Whisper |
| `_gpt_voice_route()` | 1001 | роутинг голосового намерения (6 типов) |
| `_gpt_voice_analyze()` | 1015 | аналитический ответ на query |
| `_draft_from_parsed()` | 1030 | парсинг JSON задачи из голоса |
| `on_voice()` | 1062 | главный обработчик голосовых |
| `on_log_message()` | 1275 | логирование сообщений в _CHAT_LOG |
| `_gpt_analyze()` | 1298 | поиск задач в переписке |
| `cmd_analyze()` | 1312 | команда /analyze |
| `_run_analyze()` | 1327 | runner для llm.choose_then_run |
| `_gpt_plan()` | 1380 | план для Лены+Глеба |
| `_gpt_plan_person()` | 1875 | план для конкретного человека |
| `cmd_plan()` | 1394 | команда /plan |
| `_run_plan()` | 1410 | runner для llm.choose_then_run |
| `on_history_import()` | 1459 | импорт result.json |
| `cmd_ai_check()` | 1503 | проверка OpenAI + OpenRouter |
| `cmd_start()` | 1581 | приветствие и справка |
| `cmd_menu()` | 1893 | интерактивное меню |
| `_run_menu_action()` | 1900 | обработчик действий меню |
| `_set_commands()` | 1989 | регистрация команд в Telegram |
| `main()` | 2011 | инициализация и запуск бота |

### llm_router.py (публичный API)

| Функция | Что делает |
|---|---|
| `call_llm(model_key, messages, ...)` | вызов любой модели через OpenRouter |
| `prepare_context(buf, chat_id, task)` | подготовка контекста (truncate/squeeze) |
| `resolve_model(chat_id, task, text)` | авто или выбранная модель |
| `choose_then_run(update, ctx, task)` | UI выбора модели → запуск runner |
| `register(app)` | регистрация хендлеров /model, /trim |
| `register_runner(name, fn)` | регистрация runner для task |
| `export_state() / import_state()` | сериализация настроек |
| `set_persistence(fn)` | callback для сохранения при изменениях |

---

## Как это развивает бизнес

| Проблема | Решение |
|---|---|
| Задачи теряются в чате | `/new` или голос «поставь задачу» → карточка в топике + строка в таблице + deep-link |
| Непонятно кто что делает | `/list`, кнопки статуса, `/menu → Статусы задач` |
| Забываем о дедлайнах | автодайджест в 12:00, алерт за день до срока, голос «какие дедлайны» |
| Теряем контекст переписки | `_CHAT_LOG` копит всё → голосом спрашиваем что угодно по истории |
| Нет стратегического взгляда | голос «куда развивать бизнес», «что важнее» → аналитик со знанием всей переписки и ролей |

**Итог:** бот — операционная память бизнеса. Ничего не теряется, всё структурировано, доступно голосом.
