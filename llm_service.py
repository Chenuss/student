"""
llm_service.py — сервис генерации дидактических материалов через ProxyAPI.ru.

- AsyncOpenAI с base_url=https://api.proxyapi.ru/v1
- Промпты загружаются из внешних файлов каталога prompts/ через prompt_loader
  (кэширование + авто-перечитывание по mtime + плейсхолдеры + fallback-дефолты):
    material.system — системный промпт методиста (KaTeX/Mermaid, структура конспекта)
    material.user   — шаблон user-промпта генерации
    mock.material.template / mock.context_line — шаблоны мок-режима
- Если PROXYAPI_KEY не задан — мок-режим: красивая заглушка про закон Ома
  с LaTeX-формулами и Mermaid-схемой цепи.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI

import prompt_loader
from app_logging import http_log_enabled, http_logger, llm_debug_enabled, redact_headers, truncate

# ВАЖНО: читаем .env ДО os.getenv — иначе при импорте этого модуля
# (он происходит в main.py раньше, чем там вызывается load_dotenv())
# переменные окружения ещё не будут загружены и ключ будет пустым.
load_dotenv()

log = logging.getLogger("ktp.llm")

PROXYAPI_KEY = os.getenv("PROXYAPI_KEY", "").strip()
PROXYAPI_BASE_URL = os.getenv("PROXYAPI_BASE_URL", "https://api.proxyapi.ru/v1").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek/deepseek-chat").strip()
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.4") or 0.4)
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096") or 4096)
# Большие модели (qwen-25-72b, deepseek-chat под нагрузкой шлюза) генерируют объёмный
# конспект по 80–120 с и дольше — таймаут чтения по умолчанию (~60 с) обрывал соединение.
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300") or 300)
LLM_CONNECT_TIMEOUT = float(os.getenv("LLM_CONNECT_TIMEOUT", "15") or 15)
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2") or 2)

MOCK_MODE = not bool(PROXYAPI_KEY)

_client: AsyncOpenAI | None = None


def _pretty_json(raw: bytes) -> str:
    """Тело запроса/ответа в читаемом виде (JSON с отступами), если это JSON."""
    try:
        import json

        return json.dumps(json.loads(raw.decode("utf-8")), ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 — не JSON / бинарное тело
        text = raw.decode("utf-8", errors="replace")
        return text if text else "<пустое тело>"


async def _llm_http_logging(request: httpx.Request) -> None:
    """Асинхронный event-hook httpx: пишет точное тело запроса в HTTP-журнал."""
    hlog = http_logger()
    body = await request.aread()
    hlog.info(
        "→ ЗАПРОС %s %s\n  headers: %s\n  body:\n%s",
        request.method, request.url, redact_headers(request.headers), _pretty_json(body),
    )


async def _llm_http_response_logging(response: httpx.Response) -> None:
    """Асинхронный event-hook httpx: пишет точное тело ответа в HTTP-журнал."""
    hlog = http_logger()
    try:
        body = await response.aread()
    except Exception as exc:  # noqa: BLE001
        body = f"<не удалось прочитать тело ответа: {exc}>".encode()
    note = ""
    if response.status_code >= 400:
        note = "  [ОШИБКА API — см. тело выше]"
    hlog.info(
        "← ОТВЕТ %s %s (%d)%s\n  headers: %s\n  body:\n%s",
        response.request.method, response.request.url, response.status_code, note,
        redact_headers(response.headers), _pretty_json(body),
    )


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        kwargs = dict(
            base_url=PROXYAPI_BASE_URL,
            api_key=PROXYAPI_KEY or "mock-key-no-real-call",
            timeout=httpx.Timeout(LLM_TIMEOUT, connect=LLM_CONNECT_TIMEOUT),
            max_retries=LLM_MAX_RETRIES,
        )
        if http_log_enabled():
            # Собственный транспорт с hooks — сырой HTTP-журнал всех обменов с API.
            kwargs["http_client"] = httpx.AsyncClient(
                timeout=httpx.Timeout(LLM_TIMEOUT, connect=LLM_CONNECT_TIMEOUT),
                event_hooks={
                    "request": [_llm_http_logging],
                    "response": [_llm_http_response_logging],
                },
            )
        _client = AsyncOpenAI(**kwargs)
        log.info(
            "Клиент ЛЛМ инициализирован: model=%s, base_url=%s, timeout=%.0f с "
            "(connect %.0f с), max_retries=%d, HTTP-журнал=%s",
            MODEL_NAME, PROXYAPI_BASE_URL, LLM_TIMEOUT, LLM_CONNECT_TIMEOUT,
            LLM_MAX_RETRIES, "включён" if http_log_enabled() else "выключен",
        )
    return _client


# ---------------------------------------------------------------------------
# Системный промпт — внешний файл prompts/material/system.md (prompt_loader).
# Кэшируется, перечитывается при изменении файла (mtime), при отсутствии файла
# используется встроенный fallback-дефолт из реестра prompt_loader.REGISTRY.
# Имя SYSTEM_PROMPT оставлено для совместимости (миграция/сверка в migrate_prompts.py).
# ---------------------------------------------------------------------------


def get_system_prompt() -> str:
    """Актуальный системный промпт методиста (из файла prompts/ или fallback)."""
    return prompt_loader.get_prompt("material.system")


class _LazyStr:
    """Строка-обёртка, отдающая АКТУАЛЬНЫЙ текст промпта при каждом обращении.

    Нужно для совместимости с кодом, который читал константу SYSTEM_PROMPT:
    значение всегда живое (файл мог быть отредактирован через API или вручную),
    а кэш и перечитывание по mtime обеспечивает prompt_loader.
    """

    __slots__ = ("_getter",)

    def __init__(self, getter):
        self._getter = getter

    def __str__(self) -> str:
        return self._getter()

    def __repr__(self) -> str:
        return self._getter()

    def __len__(self) -> int:
        return len(self._getter())

    def __eq__(self, other) -> bool:
        return str(self) == str(other)

    def __contains__(self, item) -> bool:
        return item in self._getter()

    def replace(self, *a, **kw):
        return self._getter().replace(*a, **kw)


SYSTEM_PROMPT = _LazyStr(get_system_prompt)  # совместимость: всегда актуальный текст


def build_user_prompt(
    lesson_title: str,
    lesson_type: str,
    topic_title: str,
    extra_prompt: str = "",
    discipline_title: str = "",
    group_name: str = "",
    group_specialty: str = "",
    plan_title: str = "",
) -> str:
    """User-промпт генерации: рендер шаблона prompts/material/user.md (плейсхолдеры {{ ... }})."""
    return prompt_loader.render_prompt(
        "material.user",
        {
            "discipline_title": discipline_title,
            "group_name": group_name,
            "group_specialty": group_specialty,
            "plan_title": plan_title,
            "topic_title": topic_title,
            "lesson_title": lesson_title,
            "lesson_type": lesson_type,
            "extra_prompt": (extra_prompt or "").strip(),
        },
    )


# ---------------------------------------------------------------------------
# Мок-режим: шаблон заглушки вынесен в prompts/mocks/material_template.txt
# (перечитывается при изменении файла; fallback — встроенный дефолт prompt_loader).
# ---------------------------------------------------------------------------


def get_mock_template() -> str:
    """Актуальный Markdown-шаблон мок-материала (из файла prompts/ или fallback)."""
    return prompt_loader.get_prompt("mock.material.template", strip=False)


MOCK_TEMPLATE = _LazyStr(get_mock_template)  # совместимость: всегда актуальный текст


async def _mock_generate(lesson_title: str, discipline_title: str = "", group_name: str = "") -> str:
    """Имитация работы модели: небольшая задержка + шаблон с адаптацией темы."""
    log.info("[МОК] Генерация для темы «%s» (PROXYAPI_KEY не задан) — реального запроса к ЛЛМ нет", lesson_title)
    delay = random.uniform(1.5, 3.0)
    if llm_debug_enabled():
        log.debug("[МОК] Имитация «генерации» %.1f с…", delay)
    await asyncio.sleep(delay)
    # Шаблон и строка контекста — из внешних файлов (prompt_loader, кэш+mtime)
    md = prompt_loader.safe_substitute_legacy(get_mock_template(), {"model": MODEL_NAME})
    ctx_bits = []
    if discipline_title:
        ctx_bits.append(f"Дисциплина: *{discipline_title}*")
    if group_name:
        ctx_bits.append(f"Группа: *{group_name}*")
    ctx_line = (" · ".join(ctx_bits) + ". ") if ctx_bits else ""
    context_suffix = prompt_loader.render_prompt(
        "mock.context_line", {"ctx_line": ctx_line, "lesson_title": lesson_title}
    )
    return md.replace("## Теоретический конспект",
                      f"## Теоретический конспект\n\n*{context_suffix}*", 1)


# ---------------------------------------------------------------------------
# Публичный интерфейс сервиса
# ---------------------------------------------------------------------------

async def generate_material(
    lesson_title: str,
    lesson_type: str,
    topic_title: str,
    extra_prompt: str = "",
    discipline_title: str = "",
    group_name: str = "",
    group_specialty: str = "",
    plan_title: str = "",
) -> str:
    """
    Генерирует дидактический материал. Возвращает Markdown-строку.

    Бросает исключение при реальной ошибке LLM (статус пары станет "error"),
    в мок-режиме всегда успешно возвращает заглушку.
    """
    if MOCK_MODE:
        return await _mock_generate(lesson_title, discipline_title, group_name)

    client = get_client()
    user_prompt = build_user_prompt(
        lesson_title=lesson_title,
        lesson_type=lesson_type,
        topic_title=topic_title,
        extra_prompt=extra_prompt,
        discipline_title=discipline_title,
        group_name=group_name,
        group_specialty=group_specialty,
        plan_title=plan_title,
    )

    debug = llm_debug_enabled()
    log.info(
        "Запрос к ЛЛМ: model=%s, base_url=%s, temperature=%s, max_tokens=%s",
        MODEL_NAME, PROXYAPI_BASE_URL, LLM_TEMPERATURE, LLM_MAX_TOKENS,
    )
    if debug:
        live_system = get_system_prompt()
        log.debug("→ system-промпт (%d симв.):\n%s", len(live_system), truncate(live_system))
        log.debug("→ user-промпт (%d симв.):\n%s", len(user_prompt), truncate(user_prompt))

    t0 = time.monotonic()
    try:
        completion = await client.chat.completions.create(
            model=MODEL_NAME,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            messages=[
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Ошибка запроса к ЛЛМ (model=%s, %.2f с): %s: %s",
            MODEL_NAME, time.monotonic() - t0, type(exc).__name__, exc,
        )
        raise
    elapsed = time.monotonic() - t0

    content = (completion.choices[0].message.content or "").strip()

    usage = getattr(completion, "usage", None)
    finish_reason = getattr(completion.choices[0], "finish_reason", None) if completion.choices else None
    if usage is not None:
        log.info(
            "Ответ ЛЛМ получен за %.2f с: prompt_tokens=%s, completion_tokens=%s, total_tokens=%s, finish_reason=%s",
            elapsed, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, finish_reason,
        )
    else:
        log.info("Ответ ЛЛМ получен за %.2f с (usage недоступен), finish_reason=%s", elapsed, finish_reason)
    if debug:
        log.debug("← ответ модели (%d симв.):\n%s", len(content), truncate(content))

    if not content:
        raise RuntimeError("Модель вернула пустой ответ")
    return content
