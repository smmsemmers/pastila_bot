"""
llm_router.py — мульти-LLM роутер для pastila_bot.

Что даёт:
  • Доступ к 30+ топовым моделям (OpenAI, Anthropic, Google, xAI, DeepSeek,
    Meta, Mistral, Qwen, Moonshot, Z-AI) через ОДИН ключ OpenRouter.
  • Единый вызов call_llm() — OpenAI-совместимый, замена прямых вызовов OpenAI.
  • Выбор модели под задачу: рекомендация + ручной выбор (/model) + 🤖 АВТО
    (дёшево → дорого по сложности запроса, без лишних вызовов модели).
  • Контекст для /plan и /analyze: обрезка 0–10 ИЛИ умное сжатие (squeeze).
    0 = не режем вообще. Всё настраивается переменными окружения — команда
    /trim полностью опциональна.
  • Сохранение выбора между перезапусками (через колбэк персистентности).

Ключи окружения:
  OPENROUTER_API_KEY  — обязательный, получить на openrouter.ai.
  OPENAI_API_KEY      — нужен только для Whisper (голос).
  TRIM_LEVEL          — 0..10, стартовый уровень обрезки (по умолчанию 4).
                        0 = не резать вообще.
  CONTEXT_MODE        — truncate | squeeze (по умолчанию truncate).

Цены в комментариях — $ за 1M токенов (вход/выход) на момент сборки (июнь 2026);
актуальные смотри на https://openrouter.ai/models. id моделей правь прямо здесь.
"""

import os
import re
import json
import logging

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

logger = logging.getLogger(__name__)

# ───────────────────── статистика токенов ─────────────────────
# Хранится в памяти до перезапуска; сбрасывается при рестарте Render.
# Ключ — model_id, значение — {"calls": int, "in": int, "out": int, "cache_read": int}
_TOKEN_STATS: dict = {}


def _record_usage(model_id: str, usage: dict):
    """Записать usage из ответа OpenRouter в накопительную статистику."""
    s = _TOKEN_STATS.setdefault(model_id, {"calls": 0, "in": 0, "out": 0, "cache_read": 0})
    s["calls"] += 1
    s["in"] += usage.get("prompt_tokens", 0)
    s["out"] += usage.get("completion_tokens", 0)
    # OpenRouter возвращает cache_read_input_tokens при Anthropic prompt caching
    s["cache_read"] += usage.get("cache_read_input_tokens", 0)


def get_token_report() -> str:
    """Сформировать текстовый отчёт по расходу токенов."""
    if not _TOKEN_STATS:
        return "📊 Статистика пуста — ни одного LLM-вызова с момента запуска."

    # Обогащаем данными о ценах
    rows = []
    for model_id, s in _TOKEN_STATS.items():
        # Ищем модель по id
        meta = next((v for v in MODELS.values() if v["id"] == model_id), None)
        label = meta["label"] if meta else model_id.split("/")[-1]
        price_in = meta["in"] if meta else 0
        price_out = meta["out"] if meta else 0
        cost_usd = (s["in"] * price_in + s["out"] * price_out) / 1_000_000
        rows.append({
            "label": label,
            "calls": s["calls"],
            "in": s["in"],
            "out": s["out"],
            "cache_read": s["cache_read"],
            "cost": cost_usd,
        })

    rows.sort(key=lambda x: x["cost"], reverse=True)
    total_cost = sum(r["cost"] for r in rows)
    total_in = sum(r["in"] for r in rows)
    total_out = sum(r["out"] for r in rows)
    total_cache = sum(r["cache_read"] for r in rows)

    lines = ["📊 <b>Расход токенов (с последнего запуска)</b>\n"]
    for r in rows:
        pct = (r["cost"] / total_cost * 100) if total_cost else 0
        cache_note = f" · кэш: {r['cache_read']:,}" if r["cache_read"] else ""
        lines.append(
            f"<b>{r['label']}</b>\n"
            f"  {r['calls']} вызовов · вход {r['in']:,} · выход {r['out']:,}{cache_note}\n"
            f"  ≈ ${r['cost']:.4f} ({pct:.0f}% расходов)"
        )

    lines.append(
        f"\n─────────────────────────\n"
        f"Итого: {total_in + total_out:,} токенов · ≈ <b>${total_cost:.4f}</b>\n"
        f"Кэш сэкономил: {total_cache:,} входных токенов"
    )

    # Рекомендация — самая дорогая модель
    if rows and rows[0]["cost"] > 0.001:
        top = rows[0]
        lines.append(
            f"\n💡 Больше всего тратит <b>{top['label']}</b> ({top['calls']} вызовов). "
            "Если не нужно глубокое мышление — можно заменить на Sonnet или Haiku."
        )

    return "\n\n".join(lines)

# ───────────────────────── ключ / эндпоинт ─────────────────────────
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OR_HEADERS_EXTRA = {
    "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://t.me/pastila_bot"),
    "X-Title": os.environ.get("OPENROUTER_TITLE", "Pastila OS bot"),
}

# ───────────────────────── реестр моделей ─────────────────────────
MODELS = {
    # ── 💎 Флагманы (максимум качества) ──
    "opus48":      {"id": "anthropic/claude-opus-4.8",             "label": "Claude Opus 4.8",      "in": 5.0,   "out": 25.0,  "tier": "flagship"},
    "gpt55":       {"id": "openai/gpt-5.5",                        "label": "GPT-5.5",              "in": 5.0,   "out": 30.0,  "tier": "flagship"},
    "gpt55pro":    {"id": "openai/gpt-5.5-pro",                    "label": "GPT-5.5 Pro",          "in": 30.0,  "out": 180.0, "tier": "flagship"},
    "gemini31pro": {"id": "google/gemini-3.1-pro-preview",         "label": "Gemini 3.1 Pro",       "in": 2.0,   "out": 12.0,  "tier": "flagship"},
    "grok43":      {"id": "x-ai/grok-4.3",                         "label": "Grok 4.3",             "in": 1.25,  "out": 2.5,   "tier": "flagship"},
    "fable5":      {"id": "anthropic/claude-fable-5",              "label": "Claude Fable 5",       "in": 10.0,  "out": 50.0,  "tier": "flagship"},

    # ── ⚖️ Сбалансированные (сильные, разумная цена) ──
    "sonnet46":    {"id": "anthropic/claude-sonnet-4.6",           "label": "Claude Sonnet 4.6",    "in": 3.0,   "out": 15.0,  "tier": "balanced"},
    "gpt54":       {"id": "openai/gpt-5.4",                        "label": "GPT-5.4",              "in": 2.5,   "out": 15.0,  "tier": "balanced"},
    "o3":          {"id": "openai/o3",                             "label": "OpenAI o3",            "in": 2.0,   "out": 8.0,   "tier": "balanced"},
    "gemini35fl":  {"id": "google/gemini-3.5-flash",              "label": "Gemini 3.5 Flash",     "in": 1.5,   "out": 9.0,   "tier": "balanced"},
    "qwen37max":   {"id": "qwen/qwen3.7-max",                      "label": "Qwen3.7 Max",          "in": 1.25,  "out": 3.75,  "tier": "balanced"},
    "glm52":       {"id": "z-ai/glm-5.2",                          "label": "GLM-5.2",              "in": 0.95,  "out": 3.0,   "tier": "balanced"},
    "kimi26":      {"id": "moonshotai/kimi-k2.6",                 "label": "Kimi K2.6",            "in": 0.66,  "out": 3.41,  "tier": "balanced"},
    "mistrall":    {"id": "mistralai/mistral-large-2512",         "label": "Mistral Large",        "in": 0.5,   "out": 1.5,   "tier": "balanced"},
    "dsv4pro":     {"id": "deepseek/deepseek-v4-pro",             "label": "DeepSeek V4 Pro",      "in": 0.435, "out": 0.87,  "tier": "balanced"},

    # ── 💸 Дешёвые и быстрые ──
    "haiku45":     {"id": "anthropic/claude-haiku-4.5",           "label": "Claude Haiku 4.5",     "in": 1.0,   "out": 5.0,   "tier": "cheap"},
    "gpt54mini":   {"id": "openai/gpt-5.4-mini",                  "label": "GPT-5.4 mini",         "in": 0.75,  "out": 4.5,   "tier": "cheap"},
    "gpt4omini":   {"id": "openai/gpt-4o-mini",                   "label": "GPT-4o mini",          "in": 0.15,  "out": 0.6,   "tier": "cheap"},
    "gpt54nano":   {"id": "openai/gpt-5.4-nano",                  "label": "GPT-5.4 nano",         "in": 0.2,   "out": 1.25,  "tier": "cheap"},
    "gemini31fl":  {"id": "google/gemini-3.1-flash-lite",        "label": "Gemini 3.1 Flash-Lite","in": 0.25,  "out": 1.5,   "tier": "cheap"},
    "llama4mav":   {"id": "meta-llama/llama-4-maverick",         "label": "Llama 4 Maverick",     "in": 0.15,  "out": 0.6,   "tier": "cheap"},
    "dsv4flash":   {"id": "deepseek/deepseek-v4-flash",          "label": "DeepSeek V4 Flash",    "in": 0.09,  "out": 0.18,  "tier": "cheap"},
    "qwen35fl":    {"id": "qwen/qwen3.5-flash-02-23",            "label": "Qwen3.5 Flash",        "in": 0.065, "out": 0.26,  "tier": "cheap"},

    # ── 🔍 Веб-исследование (встроенный поиск) ──
    "sonar_deep":   {"id": "perplexity/sonar-deep-research",        "label": "Perplexity Deep Research", "in": 2.0,   "out": 8.0,   "tier": "balanced"},
    "sonar_pro":    {"id": "perplexity/sonar-pro",                  "label": "Perplexity Sonar Pro",     "in": 3.0,   "out": 15.0,  "tier": "balanced"},

    # ── 🆓 Бесплатные (есть лимиты скорости) ──
    "ossfree":     {"id": "openai/gpt-oss-120b:free",           "label": "GPT-OSS 120B",         "in": 0.0,   "out": 0.0,   "tier": "free"},
    "qwencoderfr": {"id": "qwen/qwen3-coder:free",              "label": "Qwen3 Coder",          "in": 0.0,   "out": 0.0,   "tier": "free"},
    "qwennextfr":  {"id": "qwen/qwen3-next-80b-a3b-instruct:free","label": "Qwen3-Next 80B",      "in": 0.0,   "out": 0.0,   "tier": "free"},
}

TIER_ORDER = ["flagship", "balanced", "cheap", "free"]
TIER_TITLE = {
    "flagship": "💎 Флагманы",
    "balanced": "⚖️ Сбалансированные",
    "cheap":    "💸 Дешёвые",
    "free":     "🆓 Бесплатные",
}

AUTO_KEY = "auto"

TASK_RECOMMENDED = {
    "voice_route": "gpt4omini",
    "analyze":     "gpt4omini",
    "plan":        "sonnet46",
    "default":     "gpt4omini",
}

# ───────── состояние выбора (память процесса + персистентность) ─────────
_MODEL_CHOICE = {}    # {chat_id: {task: model_key|'auto'}}
_TRIM_LEVEL = {}      # {chat_id: 0..10}
_CONTEXT_MODE = {}    # {chat_id: 'truncate'|'squeeze'}

DEFAULT_TRIM_LEVEL = max(0, min(10, int(os.environ.get("TRIM_LEVEL", "4") or 4)))
_dm = (os.environ.get("CONTEXT_MODE", "truncate") or "truncate").lower()
DEFAULT_CONTEXT_MODE = _dm if _dm in ("truncate", "squeeze") else "truncate"

# Модель, которая делает выжимку в режиме squeeze. По умолчанию Sonnet 4.6 —
# аккуратнее пересказывает и реже теряет важное (дороже gpt-4o-mini ~в 20 раз).
# Подешевле, но всё ещё бережно: "dsv4pro" ($0.435/0.87) или "gemini35fl".
_sm = os.environ.get("SQUEEZE_MODEL", "sonnet46")
SQUEEZE_MODEL_KEY = _sm if _sm in MODELS else "sonnet46"

_persist_cb = None


def set_persistence(async_cb):
    global _persist_cb
    _persist_cb = async_cb


async def _persist():
    if _persist_cb is None:
        return
    try:
        await _persist_cb()
    except Exception as e:
        logger.warning("Не удалось сохранить состояние LLM-роутера: %s", e)


def export_state():
    return {"models": _MODEL_CHOICE, "trim": _TRIM_LEVEL, "mode": _CONTEXT_MODE}


def import_state(d):
    if not isinstance(d, dict):
        return
    _MODEL_CHOICE.clear()
    for cid, tasks in (d.get("models") or {}).items():
        try:
            _MODEL_CHOICE[int(cid)] = dict(tasks)
        except Exception:
            pass
    _TRIM_LEVEL.clear()
    for cid, lvl in (d.get("trim") or {}).items():
        try:
            _TRIM_LEVEL[int(cid)] = max(0, min(10, int(lvl)))
        except Exception:
            pass
    _CONTEXT_MODE.clear()
    for cid, mode in (d.get("mode") or {}).items():
        try:
            _CONTEXT_MODE[int(cid)] = mode if mode in ("truncate", "squeeze") else "truncate"
        except Exception:
            pass


def model_key_for(chat_id, task):
    chosen = _MODEL_CHOICE.get(chat_id, {}).get(task)
    if chosen and (chosen == AUTO_KEY or chosen in MODELS):
        return chosen
    return TASK_RECOMMENDED.get(task, TASK_RECOMMENDED["default"])


def set_model(chat_id, task, key):
    _MODEL_CHOICE.setdefault(chat_id, {})[task] = key


def get_trim_level(chat_id):
    return _TRIM_LEVEL.get(chat_id, DEFAULT_TRIM_LEVEL)


def set_trim_level(chat_id, level):
    _TRIM_LEVEL[chat_id] = max(0, min(10, int(level)))


def get_context_mode(chat_id):
    return _CONTEXT_MODE.get(chat_id, DEFAULT_CONTEXT_MODE)


def set_context_mode(chat_id, mode):
    _CONTEXT_MODE[chat_id] = mode if mode in ("truncate", "squeeze") else "truncate"


# ───────────── авто-режим: дёшево → дорого по сложности ─────────────
_HARD_MARKERS = (
    "стратег", "архитектур", "подробн", "глубок", "сложн", "обоснуй",
    "проанализируй", "сравни", "плюсы и минусы", "почему", "roadmap",
    "дорожн", "приоритизир", "распиши", "детальн",
)


def auto_pick(task, context_text=""):
    text = (context_text or "").lower()
    n = len(text)
    score = 0
    if n > 6000:
        score += 2
    elif n > 2500:
        score += 1
    if task == "plan":
        score += 1
    if any(m in text for m in _HARD_MARKERS):
        score += 1
    if score >= 3:
        return "sonnet46"
    if score >= 1:
        return "gpt4omini"
    return "dsv4flash"


def resolve_model(chat_id, task, context_text=""):
    key = model_key_for(chat_id, task)
    is_auto = key == AUTO_KEY
    if is_auto:
        key = auto_pick(task, context_text)
    m = MODELS[key]
    return m["id"], m["label"], is_auto


def model_id_for(chat_id, task, context_text=""):
    mid, _, _ = resolve_model(chat_id, task, context_text)
    return mid


# ───────────────────────── кэширование промптов ─────────────────
def sys_cached(text: str) -> dict:
    """Системный промпт с маркером кэширования (Anthropic prompt cache)."""
    return {
        "role": "system",
        "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
    }


def user_with_cache(static_ctx: str, query: str) -> dict:
    """User-сообщение: статичный блок (KB / лог) кэшируется, запрос — нет."""
    if not static_ctx:
        return {"role": "user", "content": query}
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": static_ctx, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": query},
        ],
    }


# ───────────────────────── вызов модели ─────────────────────────
async def call_llm(messages, model_id, *, temperature=0.2, max_tokens=1200,
                   response_format=None, timeout=120, thinking_budget=0):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY не задан")
    payload = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if thinking_budget > 0:
        # Extended thinking: Anthropic models via OpenRouter
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        # temperature must be 1 when thinking is enabled
        payload["temperature"] = 1
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        **_OR_HEADERS_EXTRA,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        if "usage" in data:
            _record_usage(model_id, data["usage"])
        content = data["choices"][0]["message"]["content"]
        # When thinking is enabled, content is a list of blocks
        if isinstance(content, list):
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            return "\n\n".join(text_parts)
        return content


def loads_loose(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ───────────── контекст: обрезка 0..10 + умное сжатие ─────────────
# Уровень обрезки:
#   0  — НЕ режем вообще: все реплики целиком (ограничено только _LOG_MAX в боте).
#   1  — почти ничего не трогаем (косметика);
#   5  — режем чётко;
#   10 — только суть (последние реплики + жёсткий лимит длины).
# Рычаги (для уровней 1..10):
#   keep_n    — сколько последних реплик оставить (300 → 6);
#   max_chars — до скольких символов ужимать каждую реплику (1600 → 110).
# С уровня 4 выкидываем «филлер» (ок/ага/👍…), с уровня 7 — дедуп подряд.
_FILLER = {
    "", "ок", "окей", "ok", "okay", "ага", "угу", "да", "неа", "нет",
    "+", "++", "спс", "спасибо", "пасиб", "ладно", "хорошо", "понял",
    "поняла", "принято", "ясно", "👍", "👌", "🙏", "🔥", "."
}


def _trim_params(level):
    level = max(1, min(10, int(level)))
    t = (level - 1) / 9.0
    keep_n = round(300 * (6 / 300) ** t)
    max_chars = round(1600 * (110 / 1600) ** t)
    return level, keep_n, max_chars


def _norm(s):
    return re.sub(r"[^\w]+", "", str(s).lower())


def trim_chatlog(chat_log, level):
    """Список (имя, текст) → компактный транскрипт под уровень 0..10."""
    level = max(0, min(10, int(level)))
    if level == 0:
        # ничего не режем: все реплики целиком (только схлопываем пробелы/переносы)
        out = ["{}: {}".format(name, re.sub(r'\s+', ' ', str(text)).strip()) for name, text in chat_log]
        return "\n".join(out) or "(переписки пока нет)"

    _, keep_n, max_chars = _trim_params(level)
    msgs = list(chat_log)[-keep_n:]
    out = []
    prev = None
    for name, text in msgs:
        text = re.sub(r"\s+", " ", str(text)).strip()
        n = _norm(text)
        if level >= 4 and n in _FILLER:
            continue
        if level >= 7 and n and n == prev:
            continue
        prev = n
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        out.append(f"{name}: {text}")
    return "\n".join(out) or "(переписки пока нет)"


_TRIM_DESC = {
    0:  "не режем — полный контекст",
    1:  "косметически (почти всё сохраняем)",
    2:  "слегка подрезаем",
    3:  "умеренно",
    4:  "умеренно + убираем филлер",
    5:  "чётко",
    6:  "довольно сильно",
    7:  "сильно + дедуп",
    8:  "очень сильно",
    9:  "до основного",
    10: "только суть (последние реплики)",
}


def trim_desc(level):
    return _TRIM_DESC.get(max(0, min(10, int(level))), "")


async def squeeze_chatlog(chat_log, *, level=4):
    """Умное сжатие истории в выжимку по фактам (вместо обрезки), с упором на
    сохранность важного. level пред-обрезает вход (0 = весь лог, ограничен _LOG_MAX
    в боте). Модель задаётся SQUEEZE_MODEL_KEY. На ошибке — откат к обрезанному тексту."""
    raw = trim_chatlog(chat_log, level)
    if raw == "(переписки пока нет)":
        return raw
    system = (
        "Ты сжимаешь рабочую переписку в плотную фактическую сводку для постановки "
        "задач и планирования. Главное правило: НЕ ТЕРЯТЬ ВАЖНОЕ. Лучше сохранить "
        "лишнее, чем выкинуть нужное.\n"
        "ДОСЛОВНО сохраняй, ничего не перефразируя и не округляя:\n"
        "• цифры, суммы, проценты, количества;\n"
        "• даты, сроки, дедлайны, время;\n"
        "• имена, @-теги, названия, ссылки/URL, пути, артикулы;\n"
        "• точные формулировки задач и поручений — кто, что, кому, к какому сроку.\n"
        "Структурируй по темам/людям. Каждое обязательство и договорённость — "
        "отдельным пунктом с ответственным. Сохрани открытые вопросы и спорные "
        "моменты. Выкидывай только явную воду (приветствия, смолток, реакции). "
        "Ничего не придумывай и не додумывай; если что-то непонятно — оставь как "
        "есть пометкой. Пиши по пунктам, без вступлений и выводов."
    )
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": raw},
    ]
    try:
        return await call_llm(
            msgs, MODELS[SQUEEZE_MODEL_KEY]["id"], temperature=0, max_tokens=1100)
    except Exception as e:
        logger.warning("squeeze не сработал, отдаю обрезанный текст: %s", e)
        return raw


async def prepare_context(chat_log, chat_id, task=None):
    """Готовит текст переписки для модели по настройкам чата.
    Режим squeeze применяется к /plan и /analyze; голос всегда на быстрой обрезке
    (чтобы не добавлять лишний вызов и латентность на каждое голосовое)."""
    level = get_trim_level(chat_id)
    if get_context_mode(chat_id) == "squeeze" and task != "voice_route":
        return await squeeze_chatlog(chat_log, level=level)
    return trim_chatlog(chat_log, level)


# ───────────────────────── отображение / клавиатуры ─────────────────────────
def _price_tag(m):
    if m["in"] == 0 and m["out"] == 0:
        return "free"
    return f"${m['in']:g}/{m['out']:g}"


def key_label(key):
    if key == AUTO_KEY:
        return "🤖 Авто (по сложности)"
    return MODELS.get(key, {}).get("label", key)


def key_price(key):
    if key == AUTO_KEY or key not in MODELS:
        return ""
    return _price_tag(MODELS[key])


def build_model_keyboard(chat_id, task, *, with_run=False):
    cur_key = model_key_for(chat_id, task)
    rec_key = TASK_RECOMMENDED.get(task, TASK_RECOMMENDED["default"])

    def mark(k):
        return "✅ " if k == cur_key else ("⭐ " if k == rec_key else "")

    rows = [[InlineKeyboardButton(
        mark(AUTO_KEY) + "🤖 Авто (по сложности)",
        callback_data=f"pm::{task}::{AUTO_KEY}")]]
    for tier in TIER_ORDER:
        rows.append([InlineKeyboardButton(f"— {TIER_TITLE[tier]} —", callback_data="noop")])
        line = []
        for key, m in MODELS.items():
            if m["tier"] != tier:
                continue
            line.append(InlineKeyboardButton(
                f"{mark(key)}{m['label']} {_price_tag(m)}",
                callback_data=f"pm::{task}::{key}"))
            if len(line) == 2:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
    if with_run:
        rows.append([InlineKeyboardButton("▶️ Запустить с выбранной", callback_data=f"go::{task}")])
    return InlineKeyboardMarkup(rows)


def build_trim_keyboard(chat_id):
    cur = get_trim_level(chat_id)
    rows, line = [], []
    for i in range(0, 11):
        line.append(InlineKeyboardButton(f"{'🔘' if i == cur else ''}{i}",
                                         callback_data=f"tl::{i}"))
        if len(line) == 6:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    if get_context_mode(chat_id) == "squeeze":
        rows.append([InlineKeyboardButton("✂️ Вернуть обычную обрезку", callback_data="cm::truncate")])
    else:
        rows.append([InlineKeyboardButton("🧠 Включить умное сжатие (squeeze)", callback_data="cm::squeeze")])
    return InlineKeyboardMarkup(rows)


def _context_status_text(chat_id):
    lvl = get_trim_level(chat_id)
    mode = get_context_mode(chat_id)
    if mode == "squeeze":
        m = "🧠 умное сжатие — дешёвая модель делает выжимку по фактам"
        lvl_part = ("0 — на сжатие идёт весь лог" if lvl == 0
                    else f"{lvl}/10 — перед сжатием подрезаем ({trim_desc(lvl)})")
    else:
        m = "✂️ обычная обрезка"
        lvl_part = ("0 — не режем, полный контекст" if lvl == 0
                    else f"{lvl}/10 — {trim_desc(lvl)}")
    return (
        "Контекст для /plan и /analyze (для голоса всегда быстрая обрезка):\n"
        f"• Режим: {m}\n"
        f"• Уровень: {lvl_part}\n\n"
        "Менять необязательно — настраивается и переменными TRIM_LEVEL / CONTEXT_MODE.\n"
        "0 = не резать вообще. Выбери уровень или переключи режим:"
    )


# ───────────────────────── команды и коллбэки ─────────────────────────
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = (context.args[0].lower() if context.args else "plan")
    if task not in TASK_RECOMMENDED:
        task = "plan"
    chat_id = update.effective_chat.id
    ck = model_key_for(chat_id, task)
    price = key_price(ck)
    suffix = f" ({price} за 1M)" if price else ""
    await update.message.reply_text(
        f"🧠 Модель для задачи «{task}». Сейчас: {key_label(ck)}{suffix}.\n"
        "🤖 Авто — выбирает по сложности. ⭐ — рекомендуется, ✅ — выбрана. Жми:",
        reply_markup=build_model_keyboard(chat_id, task),
    )


async def on_model_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("::")
    if len(parts) < 3:
        return
    _, task, key = parts[0], parts[1], parts[2]
    if key != AUTO_KEY and key not in MODELS:
        return
    set_model(q.message.chat_id, task, key)
    await _persist()
    pending = task in (context.chat_data.get("llm_pending") or {})
    if key == AUTO_KEY:
        body = (f"🤖 Для «{task}» включён авто-режим: модель выбирается по "
                "сложности запроса (дёшево → дорого).")
    else:
        m = MODELS[key]
        body = f"✅ Для «{task}» выбрана: {m['label']} ({_price_tag(m)} за 1M)."
    await q.edit_message_text(
        body, reply_markup=build_model_keyboard(q.message.chat_id, task, with_run=pending))


async def cmd_trim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/trim [0..10] — уровень обрезки контекста (опционально). Кнопкой — режим squeeze."""
    chat_id = update.effective_chat.id
    if context.args:
        try:
            set_trim_level(chat_id, int(context.args[0]))
            await _persist()
        except ValueError:
            pass
    await update.message.reply_text(
        _context_status_text(chat_id), reply_markup=build_trim_keyboard(chat_id))


async def on_trim_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    set_trim_level(q.message.chat_id, int(q.data.split("::")[1]))
    await _persist()
    await q.edit_message_text(
        _context_status_text(q.message.chat_id), reply_markup=build_trim_keyboard(q.message.chat_id))


async def on_context_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    set_context_mode(q.message.chat_id, q.data.split("::")[1])
    await _persist()
    await q.edit_message_text(
        _context_status_text(q.message.chat_id), reply_markup=build_trim_keyboard(q.message.chat_id))


async def on_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── «предложить лучшую модель, но дать выбор», затем запустить задачу ──
_RUNNERS = {}


def register_runner(task, coro):
    _RUNNERS[task] = coro


async def choose_then_run(update: Update, context: ContextTypes.DEFAULT_TYPE, task, pending=None):
    chat_id = update.effective_chat.id
    context.chat_data.setdefault("llm_pending", {})[task] = pending or {}
    rk = model_key_for(chat_id, task)
    price = key_price(rk)
    suffix = f" ({price} за 1M)" if price else ""
    await update.message.reply_text(
        f"🧠 Рекомендую: {key_label(rk)}{suffix} — оптимально для «{task}».\n"
        "Оставь как есть, выбери другую или 🤖 Авто, затем «Запустить»:",
        reply_markup=build_model_keyboard(chat_id, task, with_run=True),
    )


async def on_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    task = q.data.split("::")[1]
    runner = _RUNNERS.get(task)
    if runner is None:
        await q.edit_message_text("⚠️ Обработчик задачи не зарегистрирован.")
        return
    pending = (context.chat_data.get("llm_pending", {}) or {}).pop(task, {})
    await q.edit_message_text(f"⏳ Запускаю «{task}»…")
    await runner(update, context, pending)


# ───────────────────────── подключение к приложению ─────────────────────────
COMMANDS = [
    BotCommand("model", "Выбрать LLM (рекомендация / Авто / выбор)"),
    BotCommand("trim", "Контекст: обрезка 0–10 или умное сжатие"),
]


def register(app):
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("trim", cmd_trim))
    app.add_handler(CallbackQueryHandler(on_model_pick, pattern="^pm::"))
    app.add_handler(CallbackQueryHandler(on_trim_pick, pattern="^tl::"))
    app.add_handler(CallbackQueryHandler(on_context_mode, pattern="^cm::"))
    app.add_handler(CallbackQueryHandler(on_run, pattern="^go::"))
    app.add_handler(CallbackQueryHandler(on_noop, pattern="^noop$"))
    logger.info("llm_router подключён: %d моделей + Авто; обрезка %d/10, режим %s",
                len(MODELS), DEFAULT_TRIM_LEVEL, DEFAULT_CONTEXT_MODE)
