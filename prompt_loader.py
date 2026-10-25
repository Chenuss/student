"""
prompt_loader.py — загрузка системных и user-промптов из внешних файлов каталога ``prompts/``.

Возможности:
  * Кэширование содержимого в памяти (файл читается один раз до изменения).
  * Авто-перечитывание при изменении файла на диске (проверка mtime перед выдачей).
  * Плейсхолдеры ``{{ name }}`` — подстановка значений через ``render()`` / ``get(..., vars=...)``.
  * Fallback-дефолты: если файл отсутствует/пустой/не задан PROMPTS_DIR — используется
    встроенный эталонный текст (реестр ``DEFAULTS``), промпт никогда не теряется.
  * Метаданные каждого промпта лежат в front-matter самого файла (.md/.txt):
        ---
        key: material.system
        type: system | user
        version: 1
        description: ...
        placeholders: a, b, c
        ---
  * Реестр всех промптов с описаниями — ``prompts/registry.json``.

Переменные окружения (см. .env.example):
  PROMPTS_DIR      — путь к каталогу промптов (по умолчанию <проект>/prompts)
  PROMPT_AUTOLOAD  — true (по умолчанию): перечитывать файлы при изменении mtime;
                     false: держать промпты в памяти до явного reload_all().

История изменений промптов версионируется в БД (таблица ``prompt_versions``,
модель ``PromptVersionRecord`` в database.py). Запись версий выполняет API
``/api/prompts`` (main.py); сам модуль prompt_loader от БД не зависит.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from string import Template
from typing import Any, Optional

log = logging.getLogger("ktp.prompts")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = (os.getenv("PROMPTS_DIR") or os.path.join(BASE_DIR, "prompts")).strip()
_AUTOLOAD_RAW = (os.getenv("PROMPT_AUTOLOAD") or "true").strip().lower()
AUTOLOAD = _AUTOLOAD_RAW not in ("0", "false", "no", "off")

# ---------------------------------------------------------------------------
# Встроенные fallback-дефолты (эталонные тексты промптов до миграции в файлы)
# ---------------------------------------------------------------------------

_DEFAULT_MATERIAL_SYSTEM = """Ты — опытный методист и преподаватель электротехники в системе среднего профессионального образования (СПО) России.
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

_DEFAULT_MATERIAL_USER = """Дисциплина: {{ discipline_title | электротехника и электроника }} (СПО, колледж).
{% if group_name %}Целевая группа: {{ group_name }}{% if group_specialty %}, код специальности {{ group_specialty }}{% endif %}. Адаптируй пример, терминологию и производственные ситуации под профиль этой группы.
{% endif %}{% if plan_title %}КТП: {{ plan_title }}
{% endif %}Раздел КТП: {{ topic_title }}
Тема пары: {{ lesson_title }}
Тип занятия: {{ lesson_type }}
{% if extra_prompt %}Уточнение преподавателя (обязательно учти): {{ extra_prompt }}
{% endif %}Сгенерируй полный дидактический материал к этой паре."""

_DEFAULT_PPTX_SYSTEM = """
Ты — эксперт по визуализации учебных материалов и созданию презентаций для колледжей СПО.
Твоя задача — трансформировать переданный текст лекции в структуру слайдов для PowerPoint
(10–14 слайдов) в дизайн-системе «карточек» (Card UI).

Правила наполнения:
- Не перегружай текстом. Для слайдов типа "content" формулируй СТРОГО от 2 до 4 ёмких тезиса
  (по 6–10 слов каждый), чтобы они идеально помещались в карточки сетки 2x1 или 3x1.
- Первый слайд — титульный (название темы, дисциплина, группа, специальность).
- Разделы: ключевые понятия, пошаговые алгоритмы монтажа (cards), таблицы/параметры,
  формулы и нормативы с расшифровкой (formula), типичные ошибки и требования ПУЭ (comparison),
  итог и контрольные вопросы.
- Ответ выдай СТРОГО в формате валидного JSON без markdown-обёртки. Допустимые типы слайдов
  и их поля:
{
  "slides": [
    {"type": "title", "title": "Тема урока", "subtitle": "Дисциплина · группа · специальность"},
    {"type": "content", "title": "...", "points": ["Тезис 1", "Тезис 2", "Тезис 3"],
     "notes": "Заметка для преподавателя"},
    {"type": "cards", "title": "...",
     "items": [{"step": "01", "title": "Короткий заголовок", "text": "Пояснение 6–10 слов"}],
     "notes": "..."},
    {"type": "formula", "title": "...", "formula": "I_p = P / (U * cos φ)",
     "params": ["I_p — расчётный ток, А", "U — напряжение, В"], "notes": "..."},
    {"type": "comparison", "title": "...",
     "correct": "Норма ПУЭ: как делать правильно", "wrong": "Типичная ошибка: что запрещено",
     "notes": "..."}
  ]
}
- "type": "cards" — 2–4 элемента; step задай номерами "01", "02", "03"...
- "type": "formula" — params: 2–4 коротких расшифровки параметров формулы.
- "type": "comparison" — ровно два текста: correct (правильно/норма) и wrong (ошибка/запрещено).
"""

_DEFAULT_PPTX_USER = """{% if ctx_line %}Контекст: {{ ctx_line }}.

{% endif %}Тема лекции: {{ lesson_title }}

Текст лекции (Markdown-конспект):

{{ lesson_text }}

Составь структуру презентации строго по правилам системного промпта. Ответ — только валидный JSON без markdown-обёртки."""

_DEFAULT_MOCK_TEMPLATE = """## Цели занятия
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

_DEFAULT_MOCK_CONTEXT_LINE = "{% if ctx_line %}{{ ctx_line }}{% endif %}Тема пары: {{ lesson_title }}."

# ---------------------------------------------------------------------------
# Реестр промптов: key -> метаданные + fallback-дефолт
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptMeta:
    key: str
    file: str                      # относимый к PROMPTS_DIR путь (или "") = только дефолт
    ptype: str = "user"            # system | user | template
    description: str = ""
    placeholders: tuple[str, ...] = ()
    default: str = ""              # fallback-текст, если файл недоступен


REGISTRY: dict[str, PromptMeta] = {
    "material.system": PromptMeta(
        key="material.system",
        file="material/system.md",
        ptype="system",
        description="Системный промпт методиста электротехники СПО (KaTeX/Mermaid, структура конспекта)",
        default=_DEFAULT_MATERIAL_SYSTEM,
    ),
    "material.user": PromptMeta(
        key="material.user",
        file="material/user.md",
        ptype="user",
        description="User-промпт генерации дидактического материала (контекст пары/предмета/группы)",
        placeholders=(
            "discipline_title", "group_name", "group_specialty",
            "plan_title", "topic_title", "lesson_title", "lesson_type", "extra_prompt",
        ),
        default=_DEFAULT_MATERIAL_USER,
    ),
    "pptx.system": PromptMeta(
        key="pptx.system",
        file="pptx/system.md",
        ptype="system",
        description="Системный промпт конвертации конспекта в JSON-структуру слайдов (Card UI)",
        default=_DEFAULT_PPTX_SYSTEM,
    ),
    "pptx.user": PromptMeta(
        key="pptx.user",
        file="pptx/user.md",
        ptype="user",
        description="User-промпт сборки слайдов: тема + полный Markdown-конспект",
        placeholders=("ctx_line", "lesson_title", "lesson_text"),
        default=_DEFAULT_PPTX_USER,
    ),
    "mock.material.template": PromptMeta(
        key="mock.material.template",
        file="mocks/material_template.txt",
        ptype="template",
        description="Шаблон мок-материала (закон Ома) для режима без API-ключа; плейсхолдер {model}",
        placeholders=("model",),
        default=_DEFAULT_MOCK_TEMPLATE,
    ),
    "mock.context_line": PromptMeta(
        key="mock.context_line",
        file="mocks/context_line.txt",
        ptype="template",
        description="Подстрока контекста (предмет/группа/тема), встраиваемая в мок-конспект",
        placeholders=("ctx_line", "lesson_title"),
        default=_DEFAULT_MOCK_CONTEXT_LINE,
    ),
}

# Совместимость: исторические имена констант -> ключи реестра
ALIASES = {
    "SYSTEM_PROMPT": "material.system",
    "PPTX_SYSTEM_PROMPT": "pptx.system",
    "MOCK_TEMPLATE": "mock.material.template",
}


def resolve_key(key: str) -> str:
    """Ключ промпта или алиас -> канонический ключ реестра."""
    k = (key or "").strip()
    if k in REGISTRY:
        return k
    if k in ALIASES:
        return ALIASES[k]
    raise KeyError(f"Неизвестный промпт «{key}». Доступны: {', '.join(REGISTRY)}")


# ---------------------------------------------------------------------------
# Front-matter метаданных в файлах промптов
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Разделяет файл на (метаданные, тело промпта). Без front-matter -> ({}, text)."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def join_front_matter(meta: dict[str, Any], body: str) -> str:
    """Собирает файл из метаданных и тела (для сохранения через API)."""
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


# ---------------------------------------------------------------------------
# Простейший шаблонизатор: {{ var }}, {{ var | default }}, {% if var %}...{% endif %}
# ---------------------------------------------------------------------------

_IF_RE = re.compile(
    r"\{%\s*if\s+((?:(?!\{%\s*(?:if|else|endif)\s*%\}).)*?)\s*%\}"
    r"(.*?)(?:\{%\s*else\s*%\}(.*?))?\{%\s*endif\s*%\}",
    re.DOTALL,
)
_VAR_RE = re.compile(r"\{\{\s*(\w+)(?:\s*\|\s*([^}]*?))?\s*\}\}")


def render(template: str, variables: Optional[dict[str, Any]] = None) -> str:
    """Подставляет плейсхолдеры. Отсутствующие переменные -> '' (или дефолт из {{ x | дф }}).

    Поддерживаются вложенные {% if %} (условие — непустое значение переменной;
    внутри условия можно использовать другие вызовы {{ var }}), {% else %},
    логические слова ``and`` / ``or`` в условиях верхнего уровня.
    Блоки разрешаются рекурсивно от самых внутренних к внешним.
    """
    if not template:
        return ""
    v = {k: ("" if val is None else str(val)) for k, val in (variables or {}).items()}

    _AND_RE = re.compile(r"\s+and\s+")
    _OR_RE = re.compile(r"\s+or\s+")

    def _cond_true(cond_src: str) -> bool:
        # сначала подставим {{ }}-вызовы внутри самого условия
        cond_src = _VAR_RE.sub(lambda mm: v.get(mm.group(1), ""), cond_src)
        parts_all = _AND_RE.split(cond_src)
        if not all(" ".join(p.split()) for p in parts_all):
            return False
        if any(_OR_RE.search(p) for p in parts_all):
            return any(
                " ".join(o.split()) for p in parts_all for o in _OR_RE.split(p)
            )
        return True

    # самый внутренний {% if %}: условие без вложенных тегов и тело без {% if/else %}
    inner_if = re.compile(
        r"\{%\s*if\s+((?:(?!\{%).)+?)\s*%\}"
        r"((?:(?!\{%\s*(?:if|else)\s*%\}).)*?)"
        r"\{%\s*endif\s*%\}",
        re.DOTALL,
    )

    def _resolve_inner(m: re.Match) -> str:
        cond, body = m.group(1), m.group(2)
        sm = re.search(r"\{%\s*else\s*%\}", body, re.DOTALL)
        if sm:
            bthen, belse = body[: sm.start()], body[sm.end():]
        else:
            bthen, belse = body, ""
        return bthen if _cond_true(cond) else belse

    out = template
    for _ in range(20):
        nxt = inner_if.sub(_resolve_inner, out)
        if nxt == out:
            break
        out = nxt
    return _VAR_RE.sub(lambda m: v.get(m.group(1), (m.group(2) or "").strip()), out)


# ---------------------------------------------------------------------------
# Ядро: кэш + отслеживание mtime + потокобезопасность
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    content: str          # тело промпта (без front-matter)
    meta: dict[str, Any]  # метаданные из front-matter
    mtime: float          # mtime файла (или -1.0, если файл отсутствовал)
    from_default: bool = False


@dataclass
class LoadedPrompt:
    key: str
    content: str
    meta: dict[str, Any]
    path: Optional[str]
    exists: bool           # файл есть на диске
    from_default: bool     # использован fallback-дефолт
    mtime: float
    placeholders: list[str] = field(default_factory=list)
    description: str = ""
    ptype: str = "user"


class PromptLoader:
    """Загрузчик промптов из каталога PROMPTS_DIR с кэшем и авто-перечитыванием по mtime."""

    def __init__(self, prompts_dir: Optional[str] = None, autoload: Optional[bool] = None):
        self.prompts_dir = os.path.abspath(prompts_dir or PROMPTS_DIR)
        self.autoload = AUTOLOAD if autoload is None else bool(autoload)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()

    # -- пути ---------------------------------------------------------------

    def path_for(self, key: str) -> Optional[str]:
        meta = REGISTRY.get(resolve_key(key))
        if not meta or not meta.file:
            return None
        return os.path.join(self.prompts_dir, meta.file)

    # -- чтение с кэшем ------------------------------------------------------

    def _entry(self, key: str) -> _CacheEntry:
        canon = resolve_key(key)
        meta = REGISTRY[canon]
        path = self.path_for(canon)
        with self._lock:
            entry = self._cache.get(canon)
            cur_mtime = -1.0
            file_exists = False
            if path:
                try:
                    cur_mtime = os.path.getmtime(path)
                    file_exists = True
                except OSError:
                    pass

            fresh = (
                entry is not None
                and (not self.autoload or entry.mtime == cur_mtime)
                # если файла нет и запись уже coming-from-default — перечитывать нечего
            )
            if fresh and entry is not None:
                return entry

            if path and file_exists:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = f.read()
                    fm, body = split_front_matter(raw)
                    body = body.rstrip("\n")
                    if not body.strip():
                        raise ValueError("пустое тело файла")
                    entry = _CacheEntry(content=body, meta=fm, mtime=cur_mtime)
                    log.debug("Промпт «%s» загружен из %s (%d симв.)", canon, path, len(body))
                except Exception as exc:  # noqa: BLE001 — любой сбой файла -> fallback
                    log.warning(
                        "Не удалось прочитать промпт «%s» из %s (%s) — используется fallback-дефолт",
                        canon, path, exc,
                    )
                    entry = _CacheEntry(content=meta.default, meta={}, mtime=-1.0, from_default=True)
            else:
                if entry is not None and entry.from_default:
                    return entry  # уже сидим на дефолте, повторных предупреждений не надо
                if path:
                    log.warning(
                        "Файл промпта «%s» отсутствует (%s) — используется встроенный fallback-дефолт",
                        canon, path,
                    )
                entry = _CacheEntry(content=meta.default, meta={}, mtime=-1.0, from_default=True)

            self._cache[canon] = entry
            return entry

    def get(self, key: str, variables: Optional[dict[str, Any]] = None, *, strip: bool = True) -> str:
        """Текст промпта; при передаче ``variables`` — рендер плейсхолдеров."""
        entry = self._entry(key)
        text = entry.content
        if variables is not None:
            text = render(text, variables)
        return text.strip() if strip else text

    def render(self, key: str, variables: Optional[dict[str, Any]] = None) -> str:
        return self.get(key, variables)

    def reload(self, key: Optional[str] = None) -> None:
        """Сбрасывает кэш (конкретного промпта или всех)."""
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(resolve_key(key), None)

    def reload_all(self) -> None:
        self.reload(None)

    # -- информация / редактирование ----------------------------------------

    def info(self, key: str) -> LoadedPrompt:
        canon = resolve_key(key)
        meta = REGISTRY[canon]
        entry = self._entry(canon)
        path = self.path_for(canon)
        exists = bool(path) and os.path.isfile(str(path))
        ph = meta.placeholders
        if not ph:
            ph = tuple(sorted(set(_VAR_RE.findall(entry.content)) and
                              {m[0] for m in _VAR_RE.findall(entry.content)}))
        return LoadedPrompt(
            key=canon,
            content=entry.content,
            meta=entry.meta,
            path=path,
            exists=exists,
            from_default=entry.from_default,
            mtime=entry.mtime,
            placeholders=list(ph),
            description=str(entry.meta.get("description") or meta.description),
            ptype=str(entry.meta.get("type") or meta.ptype),
        )

    def list_all(self) -> list[LoadedPrompt]:
        return [self.info(k) for k in REGISTRY]

    def save(self, key: str, content: str, *, extra_meta: Optional[dict[str, Any]] = None) -> LoadedPrompt:
        """Сохраняет новый текст промпта в файл (с front-matter) и сбрасывает кэш.

        Возвращает обновлённое info. Исключение — если для ключа нет файла-хранилища.
        """
        canon = resolve_key(key)
        meta_obj = REGISTRY[canon]
        path = self.path_for(canon)
        if not path:
            raise RuntimeError(f"Для промпта «{canon}» нет файла-хранилища (только дефолт)")
        content = (content or "").rstrip("\n")
        if not content.strip():
            raise ValueError("Тело промпта не может быть пустым")

        old_info = self.info(canon)
        fm: dict[str, Any] = dict(old_info.meta) if old_info.meta else {}
        fm.setdefault("key", canon)
        fm.setdefault("type", meta_obj.ptype)
        try:
            fm["version"] = int(fm.get("version", 0)) + 1
        except (TypeError, ValueError):
            fm["version"] = 1
        fm.setdefault("description", meta_obj.description)
        if meta_obj.placeholders:
            fm["placeholders"] = ", ".join(meta_obj.placeholders)
        updated = time_str()
        fm["updated_at"] = updated
        if extra_meta:
            fm.update({k: v for k, v in extra_meta.items() if v})

        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(join_front_matter(fm, content) + "\n")
        os.replace(tmp, path)

        self.reload(canon)
        log.info("Промпт «%s» сохранён в %s (v%s, %d симв.)", canon, path, fm.get("version"), len(content))
        return self.info(canon)


def time_str() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Глобальный экземпляр + функции-прокси (экспортные имена для сервисов)
# ---------------------------------------------------------------------------

default_loader = PromptLoader()


def get_prompt(key: str, variables: Optional[dict[str, Any]] = None, *, strip: bool = True) -> str:
    return default_loader.get(key, variables, strip=strip)


def render_prompt(key: str, variables: Optional[dict[str, Any]] = None) -> str:
    return default_loader.render(key, variables)


def reload_prompts(key: Optional[str] = None) -> None:
    default_loader.reload(key)


def reload_all() -> None:
    default_loader.reload_all()


def prompt_info(key: str) -> LoadedPrompt:
    return default_loader.info(key)


def list_prompts() -> list[LoadedPrompt]:
    return default_loader.list_all()


def save_prompt(key: str, content: str, *, extra_meta: Optional[dict[str, Any]] = None) -> LoadedPrompt:
    return default_loader.save(key, content, extra_meta=extra_meta)


def ensure_files_exist(force: bool = False) -> dict[str, Any]:
    """Создаёт отсутствующие файлы промптов из fallback-дефолтов (для скрипта миграции).

    Возвращает {key: 'created' | 'exists' | 'skipped_no_file' | 'error:<...>'}.
    force=True перезаписывает существующие файлы эталонными дефолтами.
    """
    result: dict[str, Any] = {}
    for key, meta in REGISTRY.items():
        path = default_loader.path_for(key)
        if not path:
            result[key] = "skipped_no_file"
            continue
        if os.path.isfile(path) and not force:
            result[key] = "exists"
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fm = {
                "key": key,
                "type": meta.ptype,
                "version": 1,
                "description": meta.description,
                "updated_at": time_str(),
            }
            if meta.placeholders:
                fm["placeholders"] = ", ".join(meta.placeholders)
            with open(path, "w", encoding="utf-8") as f:
                f.write(join_front_matter(fm, meta.default) + "\n")
            result[key] = "overwritten" if force else "created"
        except OSError as exc:
            result[key] = f"error:{exc}"
    default_loader.reload_all()
    return result


def write_registry_json() -> str:
    """Пишет prompts/registry.json — машиночитаемый реестр промптов."""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    data = {
        "_comment": "Реестр промптов приложения. Тексты — в файлах ниже; метаданные дублируются во front-matter.",
        "prompts_dir": "prompts",
        "prompts": [
            {
                "key": k,
                "file": m.file,
                "type": m.ptype,
                "description": m.description,
                "placeholders": list(m.placeholders),
                "has_fallback_default": bool(m.default),
            }
            for k, m in REGISTRY.items()
        ],
    }
    path = os.path.join(PROMPTS_DIR, "registry.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


# -- Template-поддержка совместимости (старый синтаксис {model} в мок-шаблоне) --

def safe_substitute_legacy(text: str, mapping: dict[str, str]) -> str:
    """Подстановка одиночных фигурных скобок {name} (string.Template) без ломки JSON-блоков."""
    try:
        return Template(text).safe_substitute(**mapping)
    except (ValueError, KeyError):  # например «$»-последовательности вне шаблона
        return text


__all__ = [
    "PromptLoader",
    "PromptMeta",
    "LoadedPrompt",
    "REGISTRY",
    "ALIASES",
    "default_loader",
    "resolve_key",
    "get_prompt",
    "render_prompt",
    "reload_prompts",
    "reload_all",
    "prompt_info",
    "list_prompts",
    "save_prompt",
    "ensure_files_exist",
    "write_registry_json",
    "render",
    "split_front_matter",
    "join_front_matter",
    "safe_substitute_legacy",
    "PROMPTS_DIR",
    "AUTOLOAD",
]
