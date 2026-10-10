"""
llm_service.py — сервис генерации дидактических материалов через ProxyAPI.ru.

- AsyncOpenAI с base_url=https://api.proxyapi.ru/v1
- Системный промпт методиста электротехники СПО (требования к KaTeX/Mermaid)
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
# Context Builder — системный промпт методиста
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Ты — опытный методист и преподаватель электротехники в системе среднего профессионального образования (СПО) России.
Твоя задача — готовить полноценные дидактические материалы к конкретной паре (учебному занятию) по КТП.

Строгие требования к оформлению:
1. Формулы записывай ТОЛЬКО в LaTeX: инлайн — $...$, блочные — $$...$$. Не используй \\(...\\) и \\[...\\].
   Блочные формулы LaTeX ($$...$$) никогда не вставляй внутрь списков (не ставь перед ними дефисы, звёздочки или цифры).
   Формула всегда должна идти отдельным параграфом с пустой строкой до и после неё.
   1.1. НИКОГДА не ставь пробелы сразу после открывающего знака $ и перед закрывающим знаком $.
        Пиши строго слитно: $F$, $q_1$, а НЕ $ F $, $ q_1 $ (пробелы внутри $...$ ломают рендеринг KaTeX).
   1.2. Блочные формулы ($$...$$) всегда отделяй пустой строкой сверху И снизу. Запрещено размещать
        блочные формулы внутри элементов списков, например: «- $$...$$» — только отдельным блоком без маркера.
   1.3. Единицы измерения пиши простым текстом вне формулы (например: $F$ — сила взаимодействия, Н),
        либо используй \\text{} внутри формулы: $8.85 \\cdot 10^{-12}\\ \\text{Ф/м}$. Кириллица допустима
        только внутри \\text{...}.
   1.4. Команды LaTeX пиши с одним обратным слэшем: \\frac{U}{R}, а не \\\\frac. Не экраний знаки доллара (\\$) —
        для математики используется обычный $.
2. Электрические цепи, структурные и принципиальные схемы, блок-схемы занятий оформляй кодом в блоках ```mermaid ... ```.
   Для цепей используй синтаксис mermaid flowchart/LR с элементами: [R] резистор, [L] катушка, [C] конденсатор, источник — (" ") или battery-подобные обозначения текстом (E, U, I).
   Для блоков mermaid: текст внутри любых узлов ВСЕГДА оборачивай в двойные экранированные кавычки.
   Пример: A["Текст узла (с пояснением)"] --> B["Шина PE"].
   Запрещено использовать круглые скобки, знаки &, слэши или запятые внутри узла без внешних кавычек ["..."].
3. Структура материала:
   - ## Цели занятия (образовательная, развивающая, воспитательная)
   - ## План пары (тайминг по этапам)
   - ## Теоретический конспект (с формулами и схемами)
   - ## Расчётный пример (полное решение «дано → формулы → подстановка → ответ»)
   - ## Задания для самостоятельной работы (3 уровня сложности)
   - ## Контрольные вопросы (5–7 вопросов)
   - ## Домашнее задание
4. Уровень изложения — студенты колледжа 16–19 лет: кратко, наглядно, с производственными примерами (электрооборудование предприятия, бытовая электротехника, охрана труда при электромонтажных работах).
5. Объём — конспект на одну пару (90 минут). Пиши на русском языке в формате Markdown."""


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
    parts = []
    parts.append(f"Дисциплина: {discipline_title or 'электротехника и электроника'} (СПО, колледж).")
    if group_name:
        spec = f", код специальности {group_specialty}" if group_specialty else ""
        parts.append(f"Целевая группа: {group_name}{spec}. Адаптируй пример, терминологию и производственные ситуации под профиль этой группы.")
    if plan_title:
        parts.append(f"КТП: {plan_title}")
    parts.append(f"Раздел КТП: {topic_title}")
    parts.append(f"Тема пары: {lesson_title}")
    parts.append(f"Тип занятия: {lesson_type}")
    if extra_prompt and extra_prompt.strip():
        parts.append(f"Уточнение преподавателя (обязательно учти): {extra_prompt.strip()}")
    parts.append("Сгенерируй полный дидактический материал к этой паре.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Мок-режим: красивая заглушка (закон Ома + LaTeX + Mermaid)
# ---------------------------------------------------------------------------

MOCK_TEMPLATE = """## Цели занятия
- **Образовательная:** сформировать понимание закона Ома для участка и полной цепи, научиться рассчитывать токи в простых цепях.
- **Развивающая:** развивать навыки расчёта электрических цепей и чтения схем.
- **Воспитательная:** воспитывать культуру безопасного труда при работе с электроустановками до 1000 В.

> ⚠️ *Это мок-материал: API-ключ `PROXYAPI_KEY` не задан в `.env`. Задайте ключ ProxyAPI.ru — и генерация пойдёт через модель `{model}`.*

## План пары (90 мин)
| Этап | Время |
|---|---|
| Организационный момент, актуализация знаний | 10 мин |
| Изложение нового материала | 30 мин |
| Расчётные примеры у доски | 20 мин |
| Самостоятельная работа | 20 мин |
| Рефлексия, домашнее задание | 10 мин |

## Теоретический конспект

### Основные величины
- Сила тока: $I = \\dfrac{q}{t}$, измерительный прибор — амперметр (включается последовательно).
- Напряжение: $U = \\dfrac{A}{q}$, измерительный прибор — вольтметр (параллельно).
- Сопротивление проводника: $R = \\rho \\dfrac{l}{S}$, где $\\rho$ — удельное сопротивление материала.

### Закон Ома для участка цепи
$$I = \\frac{U}{R}$$

Сила тока на участке цепи прямо пропорциональна напряжению и обратно пропорциональна сопротивлению.

### Структурная схема простейшей цепи постоянного тока
```mermaid
flowchart LR
    E(("E<br/>источник ЭДС")) --> A["Ammeter (A)"]
    A --> R1["R1 — приёмник"]
    R1 --> K["K — ключ"]
    K --> E
    V["V — вольтметр"] -.измеряет U.- R1
```

### Закон Ома для полной цепи
$$I = \\frac{E}{R + r_0}$$

где $E$ — ЭДС источника, $r_0$ — внутреннее сопротивление. Напряжение на зажимах: $U = E - I r_0$.

```mermaid
flowchart LR
    subgraph Источник
        EMF(("E")) --- r0["r₀ — внутр. сопротивление"]
    end
    EMF --> SW["SAVD — автомат"]
    SW --> LOAD["Rн — нагрузка (электродвигатель станка)"]
    LOAD --> r0
```

## Расчётный пример
**Дано:** $E = 12~В$, $r_0 = 1~Ом$, $R = 5~Ом$. **Найти:** $I$, $U$.

Решение:
$$I = \\frac{E}{R + r_0} = \\frac{12}{5 + 1} = 2~А$$
$$U = I \\cdot R = 2 \\cdot 5 = 10~В$$

*Ответ:* ток в цепи $I = 2~А$, напряжение на нагрузке $U = 10~В$.

## Задания для самостоятельной работы
**Уровень А (базовый).** По участку цепи течёт ток $I = 0{,}5~А$ при $R = 40~Ом$. Найдите напряжение $U$.

**Уровень Б (повышенный).** Элемент с $E = 9~В$ и $r_0 = 0{,}5~Ом$ подключён к реостату. При каком $R$ ток будет $3~А$? Постройте схему в терминах Mermaid.

**Уровень В (высокий).** Участок состоит из $R_1 = 6~Ом$ и $R_2 = 3~Ом$ в параллели, включенных через амперметр к источнику $U = 12~В$. Найдите общий ток и токи в ветвях.

## Контрольные вопросы
1. Сформулируйте закон Ома для участка цепи. Как выглядит его графическая интерпретация (вольт-амперная характеристика)?
2. Почему амперметр включают последовательно, а вольтметр — параллельно?
3. Чем отличается ЭДС от напряжения на зажимах источника?
4. От каких параметров зависит сопротивление проводника?
5. Каковы признаки короткого замыкания и чем оно опасно на производстве?

## Домашнее задание
§ учебника по теме «Закон Ома», задачи 12.3–12.6; подготовить расчёт пуска асинхронного двигателя по схеме звезды (шаблон в LMS).
"""


async def _mock_generate(lesson_title: str, discipline_title: str = "", group_name: str = "") -> str:
    """Имитация работы модели: небольшая задержка + шаблон с адаптацией темы."""
    log.info("[МОК] Генерация для темы «%s» (PROXYAPI_KEY не задан) — реального запроса к ЛЛМ нет", lesson_title)
    delay = random.uniform(1.5, 3.0)
    if llm_debug_enabled():
        log.debug("[МОК] Имитация «генерации» %.1f с…", delay)
    await asyncio.sleep(delay)
    md = MOCK_TEMPLATE.replace("{model}", MODEL_NAME)
    # Подставим реальную тему пары и контекст предмета/группы в конспект
    ctx_bits = []
    if discipline_title:
        ctx_bits.append(f"Дисциплина: *{discipline_title}*")
    if group_name:
        ctx_bits.append(f"Группа: *{group_name}*")
    ctx_line = (" · ".join(ctx_bits) + ". ") if ctx_bits else ""
    md = md.replace(
        "## Теоретический конспект",
        f"## Теоретический конспект\n\n*{ctx_line}Тема пары: {lesson_title}.*",
        1,
    )
    return md


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
        log.debug("→ system-промпт (%d симв.):\n%s", len(SYSTEM_PROMPT), truncate(SYSTEM_PROMPT))
        log.debug("→ user-промпт (%d симв.):\n%s", len(user_prompt), truncate(user_prompt))

    t0 = time.monotonic()
    try:
        completion = await client.chat.completions.create(
            model=MODEL_NAME,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
