"""
pptx_service.py — сервис сборки презентаций PowerPoint (.pptx) по сгенерированному конспекту.

Пайплайн:
  1. extract_presentation_json(lesson_title, lesson_text) — отдельный запрос к LLM
     (существующий асинхронный клиент ProxyAPI из llm_service) со строгим требованием
     вернуть JSON-структуру слайдов { "slides": [ {type,title,...}, ... ] }.
  2. build_pptx_file(slides_json, output_path) — python-pptx собирает файл:
     формат 16:9, строгий дизайн (темно-синие/графитовые заголовки, Calibri/Arial),
     типы слайдов title / content / formula, заметки преподавателя в notes_slide.

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

log = logging.getLogger("ktp.pptx")

# ---------------------------------------------------------------------------
# Дизайн презентации (строгий, «учебный»)
# ---------------------------------------------------------------------------

SLIDE_W = Inches(13.333)   # 16:9
SLIDE_H = Inches(7.5)

ACCENT_DARK = RGBColor(0x1F, 0x3A, 0x5F)   # тёмно-синий — акцент заголовков
COLOR_GRAPHITE = RGBColor(0x37, 0x47, 0x4F) # графит — основной текст
COLOR_BODY = RGBColor(0x21, 0x2B, 0x36)
COLOR_MUTED = RGBColor(0x5C, 0x6B, 0x7A)
COLOR_WHITE = RGBColor(0xFF, 0xFF, 0xFF)
COLOR_BG = RGBColor(0xFA, 0xFB, 0xFC)
COLOR_FORMULA_BG = RGBColor(0xEE, 0xF2, 0xF7)
FONT_HEAD = "Calibri"
FONT_BODY = "Arial"


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


# ---------------------------------------------------------------------------
# 1. Запрос к LLM: конспект → JSON структуры слайдов
# ---------------------------------------------------------------------------

PPTX_SYSTEM_PROMPT = """
Ты — эксперт по визуализации учебных материалов и созданию презентаций для колледжей СПО.
Твоя задача — трансформировать переданный текст лекции в структуру слайдов для PowerPoint (10–14 слайдов).
Правила для слайдов:
- Не перегружай текстом: максимум 3–5 коротких тезисов на слайд (до 6-8 слов в пункте).
- Первый слайд — титульный (название темы, дисциплина, группа).
- Разделы: ключевые понятия, таблицы/параметры, формулы с расшифровкой, технологический порядок монтажа, типичные ошибки, итог и контрольные вопросы.
- Ответ выдай СТРОГО в формате валидного JSON без markdown-обёртки:
{
  "slides": [
    {
      "type": "title",
      "title": "...",
      "subtitle": "..."
    },
    {
      "type": "content",
      "title": "...",
      "points": ["Тезис 1", "Тезис 2", "Тезис 3"],
      "notes": "Заметка для преподавателя к этому слайду"
    },
    {
      "type": "formula",
      "title": "...",
      "formula_text": "I_p = P / (U * cos φ)",
      "description": "Пояснение параметров формулы"
    }
  ]
}
"""

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _parse_slides_json(raw: str) -> dict:
    """Достаёт валидный JSON из ответа модели (снимает markdown-обёртку, если модель её добавила)."""
    text = raw.strip()
    # strips ```json ... ``` fences just in case
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise ValueError("LLM не вернула корректный JSON со структурой слайдов")
        data = json.loads(m.group(0))
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

    ctx = []
    if discipline_title:
        ctx.append(f"Дисциплина: {discipline_title}")
    if group_name:
        ctx.append(f"Группа: {group_name}")
    ctx_line = ("Контекст: " + "; ".join(ctx) + ".\n\n") if ctx else ""
    user_prompt = (
        f"{ctx_line}Тема лекции: {lesson_title}\n\n"
        "Текст лекции (Markdown-конспект):\n\n"
        f"{lesson_text}\n\n"
        "Составь структуру презентации строго по правилам системного промпта. "
        "Ответ — только валидный JSON без markdown-обёртки."
    )

    debug = llm_service.llm_debug_enabled()
    log.info(
        "Запрос к ЛЛМ (слайды): model=%s, base_url=%s, конспект %d симв.",
        llm_service.MODEL_NAME, llm_service.PROXYAPI_BASE_URL, len(lesson_text),
    )
    if debug:
        log.debug("→ system-промпт слайдов:\n%s", llm_service.truncate(PPTX_SYSTEM_PROMPT))
        log.debug("→ user-промпт слайдов (%d симв.):\n%s", len(user_prompt), llm_service.truncate(user_prompt))

    t0 = time.monotonic()
    completion = await client.chat.completions.create(
        model=llm_service.MODEL_NAME,
        temperature=0.3,
        max_tokens=llm_service.LLM_MAX_TOKENS,
        messages=[
            {"role": "system", "content": PPTX_SYSTEM_PROMPT},
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
                                  f"группа {group_name}" if group_name else "") if p]
    return {
        "slides": [
            {"type": "title", "title": lesson_title,
             "subtitle": " · ".join(subtitle_parts) + " · СПО"},
            {"type": "content", "title": "План лекции",
             "points": ["Основные величины и определения", "Закон Ома для участка цепи",
                        "Закон Ома для полной цепи", "Расчётный пример", "Итоги и вопросы"],
             "notes": "Озвучьте план, акцентируйте связь с производственными ситуациями."},
            {"type": "content", "title": "Ключевые понятия",
             "points": ["Ток I = q / t", "Напряжение U = A / q", "Сопротивление R = ρ·l/S",
                        "Амперметр — последовательно", "Вольтметр — параллельно"],
             "notes": "Проверьте единицы измерения: А, В, Ом."},
            {"type": "formula", "title": "Закон Ома для участка цепи",
             "formula_text": "I = U / R",
             "description": "I — сила тока [А], U — напряжение [В], R — сопротивление [Ом].",
             "notes": "Покажите линейную ВАХ: график I(U)."},
            {"type": "formula", "title": "Закон Ома для полной цепи",
             "formula_text": "I = E / (R + r₀)",
             "description": "E — ЭДС источника [В], r₀ — внутреннее сопротивление [Ом], R — нагрузка.",
             "notes": "Объясните падение напряжения внутри источника: U = E − I·r₀."},
            {"type": "content", "title": "Параметры цепи: таблица",
             "points": ["E = 12 В, r₀ = 1 Ом", "R = 5 Ом — нагрузка", "I = E/(R+r₀) = 2 А",
                        "U = I·R = 10 В"],
             "notes": "Проведите расчёт у доски вместе со студентами."},
            {"type": "content", "title": "Типичные ошибки",
             "points": ["Амперметр включают параллельно", "Путают ЭДС и напряжение на зажимах",
                        "Забывают внутреннее сопротивление", "Неверные единицы при расчёте"],
             "notes": "Разберите последствия каждой ошибки на примере КЗ."},
            {"type": "content", "title": "Итог занятия",
             "points": ["Ток пропорционален напряжению", "Ток обратно пропорционален сопротивлению",
                        "Полная цепь учитывает r₀", "Расчёт — основа электромонтажа"],
             "notes": "Зафиксируйте выводы на доске."},
            {"type": "content", "title": "Контрольные вопросы",
             "points": ["Сформулируйте закон Ома для участка цепи", "Почему амперметр — последовательно?",
                        "Чем ЭДС отличается от U на зажимах?", "От чего зависит R проводника?",
                        "Признаки и опасность короткого замыкания"],
             "notes": "Опрос 3–4 студентов, оценки в журнал."},
        ]
    }


# ---------------------------------------------------------------------------
# 2. Сборка .pptx (python-pptx)
# ---------------------------------------------------------------------------

def _blank(pres: Presentation):
    """Макет без плейсхолдеров — всё рисуем сами (полный контроль стиля)."""
    return pres.slides.add_slide(pres.slide_layouts[6])


def _bg(slide, color: RGBColor) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _accent_bar(slide, x, y, w, h, color: RGBColor = ACCENT_DARK) -> None:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    shape.shadow.inherit = False


def _textbox(slide, x, y, w, h):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    return box, tf


def _add_notes(slide, text: str) -> None:
    notes = _clean(text)
    if not notes:
        return
    try:
        slide.notes_slide.notes_text_frame.text = notes
    except Exception:  # noqa: BLE001 — заметки не должны ломать сборку
        log.warning("Не удалось добавить заметки спикера к слайду", exc_info=True)


def _slide_title(slide, title: str) -> None:
    t = _clean(title) or "Без названия"
    _accent_bar(slide, Inches(0.55), Inches(0.42), Inches(0.14), Inches(0.62))
    box, tf = _textbox(slide, Inches(0.9), Inches(0.35), SLIDE_W - Inches(1.45), Inches(0.95))
    p = tf.paragraphs[0]
    _set_run(p.add_run(), t, 30, ACCENT_DARK, bold=True, font=FONT_HEAD)


def _build_title_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    # левая графитовая панель + акцентная полоса
    _accent_bar(slide, 0, 0, Inches(0.35), SLIDE_H, ACCENT_DARK)
    band_y = Inches(2.15)
    band = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.35), band_y,
                                  SLIDE_W - Inches(0.35), Inches(2.6))
    band.fill.solid()
    band.fill.fore_color.rgb = ACCENT_DARK
    band.line.fill.background()
    band.shadow.inherit = False

    box, tf = _textbox(pres and slide, Inches(1.0), band_y + Inches(0.35),
                       SLIDE_W - Inches(2.2), Inches(1.3))
    tf.vertical_anchor = MSO_ANCHOR.TOP
    p = tf.paragraphs[0]
    _set_run(p.add_run(), _clean(sl.get("title")) or "Презентация", 40, COLOR_WHITE,
             bold=True, font=FONT_HEAD)

    sub = _clean(sl.get("subtitle"))
    if sub:
        box2, tf2 = _textbox(slide, Inches(1.0), band_y + Inches(1.65),
                             SLIDE_W - Inches(2.2), Inches(0.8))
        p2 = tf2.paragraphs[0]
        _set_run(p2.add_run(), sub, 20, RGBColor(0xC9, 0xD6, 0xE8), font=FONT_BODY)

    foot, ftf = _textbox(slide, Inches(1.0), Inches(6.7), SLIDE_W - Inches(2.0), Inches(0.5))
    fp = ftf.paragraphs[0]
    _set_run(fp.add_run(), "Учебная презентация · Среднее профессиональное образование",
             12, COLOR_MUTED, italic=True)
    _add_notes(slide, sl.get("notes", ""))


def _build_content_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_title(slide, sl.get("title", ""))

    points = sl.get("points") or []
    if isinstance(points, str):
        points = [points]
    box, tf = _textbox(slide, Inches(1.0), Inches(1.7), SLIDE_W - Inches(2.0), Inches(5.2))
    first = True
    for raw in points[:7]:
        text = _clean(raw)
        if not text:
            continue
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(14)
        _set_run(p.add_run(), "▪ ", 20, ACCENT_DARK, bold=True)
        _set_run(p.add_run(), text, 20, COLOR_BODY, font=FONT_BODY)
    if first:  # ни одного пункта — пустой слайд недопустим
        _set_run(tf.paragraphs[0].add_run(), "Материалы слайда не заданы", 18, COLOR_MUTED)
    _add_notes(slide, sl.get("notes", ""))


def _build_formula_slide(pres: Presentation, sl: dict) -> None:
    slide = _blank(pres)
    _bg(slide, COLOR_BG)
    _slide_title(slide, sl.get("title", "Формула"))

    formula = _clean(sl.get("formula_text")) or "—"
    card_y, card_h = Inches(2.1), Inches(2.0)
    card = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1.6), card_y,
                                  SLIDE_W - Inches(3.2), card_h)
    card.fill.solid()
    card.fill.fore_color.rgb = COLOR_FORMULA_BG
    card.line.color.rgb = ACCENT_DARK
    card.line.width = Pt(1.25)
    card.shadow.inherit = False
    ctf = card.text_frame
    ctf.word_wrap = True
    ctf.vertical_anchor = MSO_ANCHOR.MIDDLE
    cp = ctf.paragraphs[0]
    cp.alignment = PP_ALIGN.CENTER
    _set_run(cp.add_run(), formula, 34, ACCENT_DARK, bold=True, font=FONT_HEAD)

    desc = _clean(sl.get("description"))
    if desc:
        box, tf = _textbox(slide, Inches(1.6), card_y + card_h + Inches(0.35),
                           SLIDE_W - Inches(3.2), Inches(2.2))
        dp = tf.paragraphs[0]
        _set_run(dp.add_run(), "Расшифровка: ", 18, COLOR_GRAPHITE, bold=True)
        _set_run(dp.add_run(), desc, 18, COLOR_BODY)
    _add_notes(slide, sl.get("notes", ""))


def build_pptx_file(slides_json: dict, output_path: str) -> str:
    """
    Собирает .pptx по структуре {"slides":[...]} и сохраняет в output_path.
    Возвращает абсолютный путь к сохранённому файлу.
    """
    slides = (slides_json or {}).get("slides") or []
    if not slides:
        raise ValueError("Нет слайдов для сборки презентации")

    pres = Presentation()
    pres.slide_width = SLIDE_W    # 16:9 — 13.333"
    pres.slide_height = SLIDE_H   # 7.5"

    builders = {"title": _build_title_slide, "content": _build_content_slide,
                "formula": _build_formula_slide}
    for i, sl in enumerate(slides[:20]):
        stype = str(sl.get("type", "content")).strip().lower()
        builder = builders.get(stype, _build_content_slide)
        if stype not in builders:
            log.warning("Слайд %d: неизвестный тип «%s» — собран как content", i + 1, stype)
        builder(pres, sl)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = output_path + ".tmp"
    pres.save(tmp_path)
    os.replace(tmp_path, output_path)  # атомарная запись: скачивание не поймёт полфайла
    abs_path = os.path.abspath(output_path)
    log.info("✔ Презентация собрана: %s слайдов → %s (%d байт)",
             len(slides[:20]), abs_path, os.path.getsize(abs_path))
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
