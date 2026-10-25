"""
pptx_service.py — сервис сборки презентаций PowerPoint (.pptx) по сгенерированному конспекту.

Пайплайн:
  1. extract_presentation_json(lesson_title, lesson_text) — отдельный запрос к LLM
     (существующий асинхронный клиент ProxyAPI из llm_service) со строгим требованием
     вернуть JSON-структуру слайдов { "slides": [ {type,title,...}, ... ] }.
     Системный и user-промпты этого шага загружаются из prompts/pptx/*.md через
     prompt_loader (кэширование, авто-перечитывание по mtime, fallback-дефолты).'
  2. build_pptx_file(slides_json, output_path) — python-pptx собирает файл в единой
     дизайн-системе «карточек» (Card UI): формат 16:9, светлый фон, акцентные бейджи,
     сетки карточек 2/3/4 колонки, формульные плашки, сравнение «Норма / Ошибка»,
     заметки преподавателя в notes_slide.

Типы слайдов: title / content / cards / formula / comparison
(неизвестные типы и старые форматы собираются как content-сетка).

В мок-режиме (PROXYAPI_KEY не задан) генерируется демонстрационная структура слайдов,
чтобы сборку .pptx можно было проверить без реального API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from html import unescape

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt

import llm_service
import prompt_loader

log = logging.getLogger("ktp.pptx")

# ---------------------------------------------------------------------------
# ЕДИНАЯ ДИЗАЙН-ПАЛИТРА (инженерная современная тема, Card UI)
# ---------------------------------------------------------------------------

SLIDE_W = Inches(13.333)   # 16:9
SLIDE_H = Inches(7.5)

# Фоны
COLOR_BG          = RGBColor(0xFF, 0xFF, 0xFF)   # чистый белый — фон контентных слайдов
COLOR_BG_SOFT     = RGBColor(0xF8, 0xFA, 0xFC)   # ультра-светлый серый (запас)
COLOR_CARD_BG     = RGBColor(0xF1, 0xF5, 0xF9)   # мягкий светло-серый — фон карточек
COLOR_CARD_LINE   = RGBColor(0xE2, 0xE8, 0xF0)   # тонкая рамка карточек

# Текст
COLOR_TEXT        = RGBColor(0x1E, 0x29, 0x3B)   # графит — основной текст (не чистый чёрный)
COLOR_MUTED       = RGBColor(0x64, 0x74, 0x8B)   # нейтральный серый — пояснения

# Акценты
COLOR_ACCENT      = RGBColor(0x1E, 0x3A, 0x8A)   # глубокий синий — заголовки, бейджи
COLOR_ACCENT_2    = RGBColor(0x25, 0x63, 0xEB)   # синий электрик — номера шагов, линии
COLOR_DANGER      = RGBColor(0xE1, 0x1D, 0x48)   # янтарно-красный — предупреждения / ПУЭ-ошибки
COLOR_OK          = RGBColor(0x15, 0x80, 0x3D)   # зелёный — «Норма / Правильно»
COLOR_OK_BG       = RGBColor(0xEC, 0xFD, 0xF5)   # светло-зелёная подложка
COLOR_DANGER_BG   = RGBColor(0xFF, 0xF1, 0xF2)   # светло-красная подложка
COLOR_FORMULA_BG  = RGBColor(0xEF, 0xF6, 0xFF)   # легкий голубой фон формульной карточки

# Титульный слайд
COLOR_TITLE_BG    = RGBColor(0x0F, 0x17, 0x2A)   # глубокий тёмно-синий фон титула
COLOR_TITLE_SUB   = RGBColor(0x93, 0xC5, 0xFD)   # светло-голубой подзаголовок
COLOR_WHITE       = RGBColor(0xFF, 0xFF, 0xFF)

FONT_HEAD = "Calibri"
FONT_BODY = "Calibri"
FONT_MONO = "Consolas"


def _clean(s: object) -> str:
    """Убираем markdown/LaTeX-разметку, допустимую в конспекте, но лишнюю на слайде."""
    if s is None:
        return ""
    t = str(s)
    t = re.sub(r"\$([^$]*)\$", r"\1", t)          # инлайн LaTeX $...$ → текст
    t = re.sub(r"`([^`]*)`", r"\1", t)            # код `...`
    t = re.sub(r"\*\*([^*]*)\*\*", r"\1", t)      # жирный
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", t)  # курсив
    t = t.replace("\\cdot", "·").replace("\\times", "×").replace("\\approx", "≈")
    t = t.replace("\\frac", "frac").replace("\\Delta", "Δ").replace("\\phi", "φ")
    t = unescape(t).strip()
    return t


def _set_run(run, text: str, size: int, color: RGBColor, *, bold: bool = False,
             italic: bool = False, font: str = FONT_BODY) -> None:
    run.text = text
    f = run.font
    f.name = font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = color


def _tight(tf, *, left: float = 0.15, right: float = 0.15,
           top: float = 0.15, bottom: float = 0.15) -> None:
    """Минимальные внутренние отступы текстового блока карточки + перенос слов."""
    tf.margin_left = Inches(left)
    tf.margin_right = Inches(right)
    tf.margin_top = Inches(top)
    tf.margin_bottom = Inches(bottom)
    tf.word_wrap = True


# ---------------------------------------------------------------------------
# 1. Запрос к LLM: конспект → JSON структуры слайдов
#    Промпты — внешние файлы prompts/pptx/system.md и prompts/pptx/user.md
#    (prompt_loader: кэш + авто-перечитывание по mtime + fallback-дефолты).
# ---------------------------------------------------------------------------


def get_pptx_system_prompt() -> str:
    """Актуальный системный промпт сборки слайдов (из файла prompts/ или fallback)."""
    return prompt_loader.get_prompt("pptx.system", strip=False)


def build_pptx_user_prompt(
    lesson_title: str,
    lesson_text: str,
    discipline_title: str = "",
    group_name: str = "",
) -> str:
    """User-промпт: рендер шаблона prompts/pptx/user.md с плейсхолдерами."""
    ctx = []
    if discipline_title:
        ctx.append(f"Дисциплина: {discipline_title}")
    if group_name:
        ctx.append(f"Группа: {group_name}")
    ctx_line = "; ".join(ctx)
    return prompt_loader.render_prompt(
        "pptx.user",
        {
            "ctx_line": ctx_line,
            "lesson_title": lesson_title,
            "lesson_text": lesson_text,
        },
    )


# Совместимость: имя константы сохранено, но значение живое (см. llm_service._LazyStr)
PPTX_SYSTEM_PROMPT = llm_service._LazyStr(get_pptx_system_prompt)


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_MD_FENCE_RE = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$", re.IGNORECASE)


def _parse_slides_json(raw: str) -> dict:
    """Достаёт валидный JSON из ответа модели (снимает markdown-обёртку ```json ... ```)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("LLM вернула пустой ответ (структура слайдов)")
    # снимает ```json ... ``` fences, если модель всё-таки добавила обёртку
    fence = _MD_FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise ValueError("LLM не вернула корректный JSON со структурой слайдов")
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Не удалось разобрать JSON-блок ответа LLM: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list) or not data["slides"]:
        raise ValueError("В ответе LLM отсутствует непустой массив «slides»")
    # нормализация типов
    for sl in data["slides"]:
        if not isinstance(sl, dict):
            raise ValueError("Элемент «slides» должен быть объектом")
        sl.setdefault("type", "content")
        sl.setdefault("title", "")
    return data


async def extract_presentation_json(
    lesson_title: str,
    lesson_text: str,
    discipline_title: str = "",
    group_name: str = "",
) -> dict:
    """
    Отправляет Markdown-конспект в LLM и возвращает словарь {"slides": [...]} .

    Бросает исключение при ошибке API или невалидном JSON.
    В мок-режиме возвращает демонстрационную структуру (без реального запроса).
    """
    if llm_service.MOCK_MODE:
        log.info("[МОК] Генерация структуры слайдов для «%s» — реального запроса к ЛЛМ нет", lesson_title)
        await asyncio.sleep(0.8)
        return _mock_slides_json(lesson_title, discipline_title, group_name)

    client = llm_service.get_client()

    # Промпты — из внешних файлов через prompt_loader (плейсхолдеры {{ ... }})
    user_prompt = build_pptx_user_prompt(
        lesson_title=lesson_title,
        lesson_text=lesson_text,
        discipline_title=discipline_title,
        group_name=group_name,
    )

    debug = llm_service.llm_debug_enabled()
    log.info(
        "Запрос к ЛЛМ (слайды): model=%s, base_url=%s, конспект %d симв.",
        llm_service.MODEL_NAME, llm_service.PROXYAPI_BASE_URL, len(lesson_text),
    )
    if debug:
        live_system = get_pptx_system_prompt()
        log.debug("→ system-промпт слайдов:\n%s", llm_service.truncate(live_system))
        log.debug("→ user-промпт слайдов (%d симв.):\n%s", len(user_prompt), llm_service.truncate(user_prompt))

    t0 = time.monotonic()
    completion = await client.chat.completions.create(
        model=llm_service.MODEL_NAME,
        temperature=0.3,
        max_tokens=llm_service.LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": get_pptx_system_prompt()},
            {"role": "user", "content": user_prompt},
        ],
    )
    elapsed = time.monotonic() - t0
    content = (completion.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("Модель вернула пустой ответ (структура слайдов)")
    log.info("JSON слайдов получен за %.2f с (%d симв.)", elapsed, len(content))
    if debug:
        log.debug("← ответ модели (слайды):\n%s", llm_service.truncate(content))

    return _parse_slides_json(content)


def _mock_slides_json(lesson_title: str, discipline_title: str, group_name: str) -> dict:
    subtitle_parts = [p for p in (discipline_title or "Электротехника и электроника",
                                  f"группа {group_name}" if group_name else "",
                                  "специальность 13.02.11 «Электрические сети»") if p]
    return {
        "slides": [
            {"type": "title", "title": lesson_title,
             "subtitle": " · ".join(subtitle_parts)},
            {"type": "content", "title": "План лекции",
             "points": ["Основные величины и определения цепи",
                        "Закон Ома для участка и полной цепи",
                        "Расчётный пример parameters",
                        "Итоги и контрольные вопросы"],
             "notes": "Озвучьте план, акцентируйте связь с производственными ситуациями."},
            {"type": "cards", "title": "Ключевые величины цепи",
             "items": [
                 {"step": "01", "title": "Сила тока", "text": "I = q / t, измеряется в амперах (А)"},
                 {"step": "02", "title": "Напряжение", "text": "U = A / q, измеряется в вольтах (В)"},
                 {"step": "03", "title": "Сопротивление", "text": "R = ρ·l / S, измеряется в омах (Ом)"},
             ],
             "notes": "Проверьте единицы измерения: А, В, Ом."},
            {"type": "formula", "title": "Закон Ома для участка цепи",
             "formula": "I = U / R",
             "params": ["I — сила тока на участке цепи, А",
                        "U — напряжение на участке, В",
                        "R — сопротивление участка, Ом"],
             "notes": "Покажите линейную ВАХ: график I(U)."},
            {"type": "formula", "title": "Закон Ома для полной цепи",
             "formula": "I = E / (R + r₀)",
             "params": ["E — ЭДС источника, В", "r₀ — внутреннее сопротивление, Ом",
                        "R — сопротивление нагрузки, Ом"],
             "notes": "Объясните падение напряжения внутри источника: U = E − I·r₀."},
            {"type": "cards", "title": "Порядок электромонтажа",
             "items": [
                 {"step": "01", "title": "Разметка", "text": "Разметка трасс согласно проекту и ПУЭ"},
                 {"step": "02", "title": "Крепление", "text": "Монтаж коробов, лотков и крепежа"},
                 {"step": "03", "title": "Прокладка", "text": "Укладка кабелей, разделка и маркировка"},
                 {"step": "04", "title": "Проверка", "text": "Измерение изоляции, пробный пуск"},
             ],
             "notes": "Подчеркните: работы со снятием напряжения, допуск по наряду."},
            {"type": "comparison", "title": "Подключение измерительных приборов",
             "correct": "Амперметр включается ТОЛЬКО последовательно в разрыв цепи; "
                        "предел измерения выбирается с запасом по расчётному току.",
             "wrong": "Амперметр включён параллельно нагрузке — почти короткое замыкание, "
                      "прибор сгорает, цепь отключается защитой.",
             "notes": "Разберите последствия ошибки на примере КЗ и срабатывания защиты."},
            {"type": "content", "title": "Типичные ошибки при расчёте",
             "points": ["Путают ЭДС и напряжение на зажимах",
                        "Забывают внутреннее сопротивление источника",
                        "Неверные единицы при подстановке в формулу"],
             "notes": "Разберите каждую ошибку на числовом примере."},
            {"type": "content", "title": "Итог занятия",
             "points": ["Ток пропорционален напряжению участка",
                        "Ток обратно пропорционален сопротивлению",
                        "Полная цепь учитывает r₀ источника"],
             "notes": "Зафиксируйте выводы на доске."},
            {"type": "cards", "title": "Контрольные вопросы",
             "items": [
                 {"step": "01", "title": "Формулировка", "text": "Сформулируйте закон Ома для участка цепи"},
                 {"step": "02", "title": "Приборы", "text": "Почему амперметр включают последовательно?"},
                 {"step": "03", "title": "Практика", "text": "Признаки и опасность короткого замыкания"},
             ],
             "notes": "Опрос 3–4 студентов, оценки в журнал."},
        ]
    }


# ---------------------------------------------------------------------------
# 2. Сборка .pptx (python-pptx) — дизайн-система карточек (Card UI)
# ---------------------------------------------------------------------------

def _blank(pres: Presentation):
    """Макет без плейсхолдеров — всё рисуем сами (полный контроль стиля)."""
    return pres.slides.add_slide(pres.slide_layouts[6])


def _bg(slide, color: RGBColor) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _accent_bar(slide, x, y, w, h, color: RGBColor = COLOR_ACCENT) -> None:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _textbox(slide, x, y, w, h):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    return box, tf


def _card(slide, x, y, w, h, *,
          fill: RGBColor = COLOR_CARD_BG,
          line: RGBColor = COLOR_CARD_LINE,
          line_w: float = 1.0):
    """Карточка со скруглёнными углами — базовый элемент дизайн-системы."""
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = line
    shape.line.width = Pt(line_w)
    shape.shadow.inherit = False
    try:  # небольшой радиус скругления (если поддерживается сборкой)
        shape.adjustments[0] = 0.06
    except Exception:  # noqa: BLE001
        pass
    tf = shape.text_frame
    _tight(tf)
    tf.clear()
    return shape, tf


def _badge(slide, x, y, text: str, *, fg: RGBColor = COLOR_ACCENT,
           bg: RGBColor | None = None, size: int = 10) -> None:
    """Компактный цветной бейдж-категория (капс) в шапке слайда."""
    label = (text or "").upper()
    w = Inches(max(1.4, 0.105 * len(label) + 0.3))
    h = Inches(0.32)
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.shadow.inherit = False
    if bg is not None:
        shape.fill.solid()
        shape.fill.fore_color.rgb = bg
    else:  # полупрозрачная подложка поверх цвета слайда
        shape.fill.background()
    shape.line.color.rgb = fg
    shape.line.width = Pt(1.0)
    try:
        shape.adjustments[0] = 0.5
    except Exception:  # noqa: BLE001
        pass
    tf = shape.text_frame
    _tight(tf, left=0.05, right=0.05, top=0.02, bottom=0.02)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _set_run(p.add_run(), label, size, fg, bold=True, font=FONT_HEAD)


def _add_notes(slide, text: str) -> None:
    notes = _clean(text)
    if not notes:
        return
    try:
        slide.notes_slide.notes_text_frame.text = notes
    except Exception:  # noqa: BLE001 — заметки не должны ломать сборку
        log.warning("Не удалось добавить заметки спикера к слайду", exc_info=True)


def _slide_header(slide, title: str, badge_text: str = "ЭЛЕКТРОМОНТАЖ • СПО",
                  badge_color: RGBColor = COLOR_ACCENT) -> None:
    """Единообразная шапка контентного слайда: бейдж → заголовок → акцентная линия."""
    # компактный бейдж-категория (Top: 0.6", Left: 0.8")
    _badge(slide, Inches(0.8), Inches(0.6), badge_text, fg=badge_color,
           bg=RGBColor(0xE8, 0xEE, 0xF9))
    # крупный заголовок (26–28pt, жирный, графит)
    t = _clean(title) or "Без названия"
    box, tf = _textbox(slide, Inches(0.8), Inches(1.02), SLIDE_W - Inches(1.6), Inches(0.72))
    _tight(tf, left=0.0, right=0.0, top=0.0, bottom=0.0)
    p = tf.paragraphs[0]
    _set_run(p.add_run(), t, 27, COLOR_TEXT, bold=True, font=FONT_HEAD)
    # тонкая декоративная акцентная линия на всю ширину рабочей области
    _accent_bar(slide, Inches(0.8), Inches(1.82), SLIDE_W - Inches(1.6), Pt(2.4),
                COLOR_ACCENT_2)


# ----------------------------- Титульный слайд -----------------------------

def _build_title_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_TITLE_BG)                       # глубокий тёмно-синий фон
    _accent_bar(slide, 0, 0, SLIDE_W, Inches(0.14), COLOR_ACCENT_2)  # верхняя неоновая нить

    _badge(slide, Inches(0.9), Inches(1.15), "УЧЕБНАЯ ПРЕЗЕНТАЦИЯ • СПО",
           fg=COLOR_TITLE_SUB, bg=None, size=11)

    # Заголовок темы — контрастный белый 36–40pt Bold
    box, tf = _textbox(slide, Inches(0.9), Inches(2.15), SLIDE_W - Inches(1.8), Inches(2.0))
    _tight(tf, left=0.0, right=0.0, top=0.0, bottom=0.0)
    p = tf.paragraphs[0]
    _set_run(p.add_run(), _clean(sl.get("title")) or "Презентация", 38, COLOR_WHITE,
             bold=True, font=FONT_HEAD)

    # Подзаголовок (дисциплина, группа, специальность) — светло-голубой 18pt
    sub = _clean(sl.get("subtitle"))
    if sub:
        box2, tf2 = _textbox(slide, Inches(0.9), Inches(4.35), SLIDE_W - Inches(1.8), Inches(0.8))
        _tight(tf2, left=0.0, right=0.0, top=0.0, bottom=0.0)
        p2 = tf2.paragraphs[0]
        _set_run(p2.add_run(), sub, 18, COLOR_TITLE_SUB, font=FONT_BODY)

    # Нижняя аккуратная плашка: год и учреждение
    year = time.localtime().tm_year
    plate, ptf = _card(slide, Inches(0.9), Inches(6.35), Inches(6.4), Inches(0.72),
                       fill=RGBColor(0x1E, 0x29, 0x3B), line=RGBColor(0x33, 0x41, 0x55))
    _tight(ptf, left=0.2, right=0.15, top=0.05, bottom=0.05)
    ptf.vertical_anchor = MSO_ANCHOR.MIDDLE
    pp = ptf.paragraphs[0]
    _set_run(pp.add_run(), f"© {year}  ·  ", 13, COLOR_TITLE_SUB, bold=True)
    _set_run(pp.add_run(), "Среднее профессиональное образование · Электромонтажная специальность",
             13, RGBColor(0xCB, 0xD5, 0xE1))
    _add_notes(slide, sl.get("notes", ""))


# --------------------- Слайд тезисов: карточная сетка ----------------------

def _grid_geometry(n: int):
    """Координаты колонок сетки карточек для n=2..4 (и вертикальная лента для n>4)."""
    margin_x = Inches(0.8)
    gap = Inches(0.3)
    work_w = SLIDE_W - margin_x * 2
    top = Inches(2.15)
    avail_h = SLIDE_H - top - Inches(0.55)
    if 2 <= n <= 4:
        w = Emu(int((work_w - gap * (n - 1)) / n))
        return [(Emu(int(margin_x + i * (w + gap))), top, w, avail_h) for i in range(n)]
    # fallback: 2 колонки строками
    cols = 2
    rows = (n + cols - 1) // cols
    w = Emu(int((work_w - gap) / cols))
    rh = Emu(int((avail_h - gap * (rows - 1)) / rows))
    cells = []
    for i in range(n):
        r, c = divmod(i, cols)
        cells.append((Emu(int(margin_x + c * (w + gap))),
                      Emu(int(top + r * (rh + gap))), w, rh))
    return cells


def _build_content_slide(pres: Presentation, sl: dict) -> None:
    """Тезисы — НЕ сплошным списком, а горизонтальной сеткой карточек (2–4 колонки)."""
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_header(slide, sl.get("title", ""))

    points = sl.get("points") or []
    if isinstance(points, str):
        points = [points]
    texts = [_clean(p) for p in points]
    texts = [t for t in texts if t][:6]

    if not texts:
        box, tf = _textbox(slide, Inches(0.8), Inches(2.4), SLIDE_W - Inches(1.6), Inches(1.0))
        _set_run(tf.paragraphs[0].add_run(), "Материалы слайда не заданы", 18, COLOR_MUTED,
                 italic=True)
        _add_notes(slide, sl.get("notes", ""))
        return

    # более длинные тексты допускаем только когда карточек мало
    cells = _grid_geometry(len(texts))
    for idx, ((x, y, w, h), text) in enumerate(zip(cells, texts), start=1):
        shape, tf = _card(slide, x, y, w, h)
        # верхний акцентный «грибок» карточки
        _accent_bar(slide, x + Inches(0.25), y, w - Inches(0.5), Pt(3), COLOR_ACCENT_2)
        tf.vertical_anchor = MSO_ANCHOR.TOP
        p_num = tf.paragraphs[0]
        _set_run(p_num.add_run(), f"{idx:02d}", 20, COLOR_ACCENT_2, bold=True, font=FONT_HEAD)
        p_txt = tf.add_paragraph()
        p_txt.space_before = Pt(8)
        _set_run(p_txt.add_run(), text, 15, COLOR_TEXT, font=FONT_BODY)
    _add_notes(slide, sl.get("notes", ""))


# ----------------------- Карточки шагов (type: cards) ----------------------

def _build_cards_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_header(slide, sl.get("title", "Пошаговый порядок"))

    items = sl.get("items") or sl.get("cards") or []
    if isinstance(items, str):
        items = [items]
    norm = []
    for it in items:
        if isinstance(it, dict):
            norm.append({
                "step": _clean(it.get("step")) or "",
                "title": _clean(it.get("title")),
                "text": _clean(it.get("text")) or _clean(it.get("description")),
            })
        elif it:
            norm.append({"step": "", "title": "", "text": _clean(it)})
    norm = [c for c in norm if c["title"] or c["text"]][:4]

    if not norm:
        _build_content_slide(pres, {**sl, "points": sl.get("points") or []})
        return

    cells = _grid_geometry(len(norm))
    for i, ((x, y, w, h), item) in enumerate(zip(cells, norm)):
        step = item["step"] or f"{i + 1:02d}"
        shape, tf = _card(slide, x, y, w, h)
        _accent_bar(slide, x, y, Pt(3.5), h, COLOR_ACCENT)  # левая синяя полоса-акцент
        tf.vertical_anchor = MSO_ANCHOR.TOP
        p_step = tf.paragraphs[0]
        _set_run(p_step.add_run(), step, 20, COLOR_ACCENT_2, bold=True, font=FONT_HEAD)
        if item["title"]:
            p_t = tf.add_paragraph()
            p_t.space_before = Pt(6)
            _set_run(p_t.add_run(), item["title"], 15, COLOR_ACCENT, bold=True, font=FONT_HEAD)
        if item["text"]:
            p_b = tf.add_paragraph()
            p_b.space_before = Pt(5)
            _set_run(p_b.add_run(), item["text"], 13.5, COLOR_TEXT, font=FONT_BODY)
    _add_notes(slide, sl.get("notes", ""))


# ------------------------ Формула / норматив (formula) ---------------------

def _split_params(desc: str) -> list[str]:
    """Разбивает старое поле description на отдельные расшифровки параметров."""
    parts = re.split(r";|,\s+(?=[A-Za-zА-Яа-яЁё$\w]+\s*[—–-])", desc or "")
    return [p.strip(" .,") for p in parts if p and p.strip(" .,")]


def _build_formula_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_header(slide, sl.get("title", "Формула"))

    formula = _clean(sl.get("formula") or sl.get("formula_text")) or "—"

    # широкая акцентная карточка по центру: голубой фон + синяя рамка
    card_y, card_h = Inches(2.25), Inches(2.1)
    card_w = SLIDE_W - Inches(2.4)
    shape, tf = _card(slide, Inches(1.2), card_y, card_w, card_h,
                      fill=COLOR_FORMULA_BG, line=COLOR_ACCENT_2, line_w=1.75)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    _set_run(p.add_run(), formula, 40, COLOR_ACCENT, bold=True, font=FONT_MONO)

    # параметры формулы — список из params[] или разбор старого description
    params = sl.get("params") or sl.get("parameters") or []
    if isinstance(params, str):
        params = [params]
    param_texts = [_clean(x) for x in params]
    param_texts = [x for x in param_texts if x]
    if not param_texts:
        param_texts = _split_params(_clean(sl.get("description")))

    if param_texts:
        top = card_y + card_h + Inches(0.4)
        avail_h = SLIDE_H - top - Inches(0.5)
        margin_x = Inches(1.2)
        gap = Inches(0.35)
        work_w = SLIDE_W - margin_x * 2
        cols = min(2, len(param_texts))
        col_w = Emu(int((work_w - gap * (cols - 1)) / cols))
        rows = (len(param_texts) + cols - 1) // cols
        cell_h = Emu(int((avail_h - Inches(0.12) * (rows - 1)) / rows))
        for i, text in enumerate(param_texts):
            r, c = divmod(i, cols)
            x = Emu(int(margin_x + c * (col_w + gap)))
            y = Emu(int(top + r * (cell_h + Inches(0.12))))
            _, ptf = _card(slide, x, y, col_w, cell_h,
                           fill=COLOR_BG_SOFT, line=COLOR_CARD_LINE)
            ptf.vertical_anchor = MSO_ANCHOR.MIDDLE
            pp = ptf.paragraphs[0]
            dash = re.split(r"[—–:]", text, maxsplit=1)
            if len(dash) == 2 and len(dash[0]) <= 24:
                _set_run(pp.add_run(), dash[0].strip() + " — ", 14, COLOR_ACCENT,
                         bold=True, font=FONT_HEAD)
                _set_run(pp.add_run(), dash[1].strip(), 14, COLOR_TEXT, font=FONT_BODY)
            else:
                _set_run(pp.add_run(), text, 14, COLOR_TEXT, font=FONT_BODY)
    _add_notes(slide, sl.get("notes", ""))


# -------------------- Сравнение «Норма / Ошибка» (comparison) --------------

def _build_comparison_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_header(slide, sl.get("title", "Правильно против ошибки"))

    correct = _clean(sl.get("correct") or sl.get("right") or sl.get("norm"))
    wrong = _clean(sl.get("wrong") or sl.get("error") or sl.get("mistake"))

    top = Inches(2.25)
    gap = Inches(0.4)
    margin_x = Inches(0.8)
    half_w = Emu(int((SLIDE_W - margin_x * 2 - gap) / 2))
    panel_h = SLIDE_H - top - Inches(0.6)
    panels = [
        (margin_x, "✔ НОРМА ПУЭ / МОНТАЖ ПО СТАНДАРТУ", correct or "—",
         COLOR_OK, COLOR_OK_BG),
        (Emu(int(margin_x + half_w + gap)), "✖ ТИПИЧНАЯ ОШИБКА / ЗАПРЕЩЕНО", wrong or "—",
         COLOR_DANGER, COLOR_DANGER_BG),
    ]
    for x, header, body, accent, soft_bg in panels:
        # панель-карточка с цветной рамкой
        shape, tf = _card(slide, x, top, half_w, panel_h,
                          fill=COLOR_BG_SOFT, line=accent, line_w=1.75)
        tf.vertical_anchor = MSO_ANCHOR.TOP
        # верхний цветной заголовок-плашка внутри панели
        head_h = Inches(0.55)
        hs, htf = _card(slide, Emu(int(x + Inches(0.18))), Emu(int(top + Inches(0.18))),
                        Emu(int(half_w - Inches(0.36))), head_h,
                        fill=soft_bg, line=accent, line_w=1.0)
        htf.vertical_anchor = MSO_ANCHOR.MIDDLE
        hp = htf.paragraphs[0]
        _set_run(hp.add_run(), header, 13, accent, bold=True, font=FONT_HEAD)
        # текст панели
        bp = tf.add_paragraph()
        bp.space_before = Pt(14)
        _set_run(bp.add_run(), body, 15, COLOR_TEXT, font=FONT_BODY)
    _add_notes(slide, sl.get("notes", ""))


# ---------------------------------------------------------------------------
# Диспетчер сборки
# ---------------------------------------------------------------------------

BUILDERS = {
    "title": _build_title_slide,
    "content": _build_content_slide,
    "cards": _build_cards_slide,
    "formula": _build_formula_slide,
    "comparison": _build_comparison_slide,
}


def build_pptx_file(slides_json: dict, output_path: str) -> str:
    """
    Собирает .pptx по структуре {"slides":[...]} и сохраняет в output_path
    в дизайн-системе карточек (Card UI). Возвращает абсолютный путь к файлу.
    """
    slides = (slides_json or {}).get("slides") or []
    if not slides:
        raise ValueError("Нет слайдов для сборки презентации")

    pres = Presentation()
    pres.slide_width = SLIDE_W    # 16:9 — 13.333"
    pres.slide_height = SLIDE_H   # 7.5"

    for i, sl in enumerate(slides[:20]):
        if not isinstance(sl, dict):
            log.warning("Слайд %d: не объект — пропущен", i + 1)
            continue
        stype = str(sl.get("type", "content")).strip().lower()
        builder = BUILDERS.get(stype)
        if builder is None:
            # эвристика: presence of items/correct/formula → нужный шаблон
            if sl.get("items"):
                builder = _build_cards_slide
            elif sl.get("correct") or sl.get("wrong"):
                builder = _build_comparison_slide
            elif sl.get("formula") or sl.get("formula_text"):
                builder = _build_formula_slide
            else:
                builder = _build_content_slide
            log.warning("Слайд %d: неизвестный тип «%s» — собран по содержимому", i + 1, stype)
        builder(pres, sl)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = output_path + ".tmp"
    pres.save(tmp_path)
    os.replace(tmp_path, output_path)  # атомарная запись: скачивание не поймёт полфайла
    abs_path = os.path.abspath(output_path)
    log.info("✔ Презентация собрана: %d слайдов → %s (%d байт)",
             len(pres.slides._sldIdLst), abs_path, os.path.getsize(abs_path))
    return abs_path


# ---------------------------------------------------------------------------
# Проверка целостности готового файла (используется после сборки)
# ---------------------------------------------------------------------------

def validate_pptx(path: str) -> int:
    """Открывает собранный файл python-pptx ещё раз; возвращает число слайдов (иначе исключение)."""
    pres = Presentation(path)
    n = len(pres.slides)
    if n == 0:
        raise RuntimeError("В собранном файле нет слайдов")
    return n
