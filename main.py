"""
main.py (v2) — FastAPI приложение «Генератор дидактических материалов и КТП».

Справочники:
  GET    /api/groups                    — список учебных групп
  POST   /api/groups                    — создать группу
  PUT    /api/groups/{id} / DELETE      — редактирование/удаление группы
  GET    /api/disciplines               — список предметов
  POST   /api/disciplines              — создать предмет
  PUT    /api/disciplines/{id} / DELETE

КТП (планы):
  GET    /api/plans                     — список всех КТП (предмет + группа)
  POST   /api/plans                     — создать КТП (предмет, группа, семестр)
  GET    /api/plans/{id}                — полная структура плана (разделы → пары)
  PUT    /api/plans/{id} / DELETE
  POST   /api/plans/{id}/topics         — добавить раздел/тему
  PUT    /api/topics/{id} / DELETE      — редактирование/удаление темы
  POST   /api/topics/{id}/lessons       — добавить занятие в тему
  PUT    /api/lessons/{id}              — редактировать пару (название/тип/дата/ДЗ)
  DELETE /api/lessons/{id}              — удалить пару из плана

Материалы (без изменений v1):
  GET    /api/ktp                       —Compat: все планы как одно дерево
  GET    /api/lessons/{id}              — данные пары + последняя версия материала
  POST   /api/lessons/{id}/generate     — фоновая генерация (BackgroundTasks)
  GET    /api/lessons/{id}/status       — polling статуса
  POST   /api/lessons/{id}/save         — ручное сохранение (новая версия)
  GET/POST /api/settings                — email преподавателя

Презентация PowerPoint (.pptx):
  POST   /api/lessons/{id}/presentation/generate  — конспект → JSON слайдов (LLM) → сборка .pptx в фоне
  GET    /api/lessons/{id}/presentation/status    — статус: idle | generating | ready | error
  GET    /api/lessons/{id}/presentation/download  — скачивание готового файла (.pptx, FileResponse)
"""

from __future__ import annotations

import truststore
truststore.inject_into_ssl()

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.requests import Request

import llm_service
import pptx_service
from database import (
    LESSON_TYPES,
    AppSettings,
    Discipline,
    Group,
    KTPPlan,
    KtpLesson,
    MaterialVersion,
    RpdTopic,
    init_db,
)
from app_logging import setup_logging
from notifier import send_completion_email, smtp_configured

load_dotenv()
setup_logging()  # консоль + файл по переменным LOG_LEVEL / LOG_FILE (см. .env.example)
log = logging.getLogger("ktp-app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Папка собранных презентаций PowerPoint: storage/presentations/lesson_{id}.pptx
PRESENTATIONS_DIR = os.getenv("PRESENTATIONS_DIR", os.path.join(BASE_DIR, "storage", "presentations"))


def _presentation_path(lesson_id: int) -> str:
    return os.path.join(PRESENTATIONS_DIR, f"lesson_{lesson_id}.pptx")


# ---------------------------------------------------------------------------
# Lifecycle: инициализация БД при старте
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info(
        "БД готова. Режим LLM: %s (модель: %s). SMTP: %s",
        "MOCK (ключ не задан)" if llm_service.MOCK_MODE else f"ProxyAPI ({llm_service.PROXYAPI_BASE_URL})",
        llm_service.MODEL_NAME,
        "включён" if smtp_configured() else "выключен",
    )
    yield


app = FastAPI(title="Генератор дидактических материалов по электротехнике", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Сессии SQLAlchemy (DI)
# ---------------------------------------------------------------------------

def get_db():
    from database import engine

    db = Session(engine)
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pydantic-схемы
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = ""
    # Алиас из интерфейса: «Уточнение преподавателя / Промпт» (user_prompt_override)
    user_prompt_override: Optional[str] = None
    email: Optional[str] = None  # куда продублировать готовый материал

    @property
    def effective_prompt(self) -> str:
        """Текст промпта, введённый преподавателем (из любого поля)."""
        return (self.user_prompt_override or self.prompt or "").strip()


class SaveRequest(BaseModel):
    content: str


class SettingsRequest(BaseModel):
    email_for_notifications: str = ""
    notify_email_enabled: bool = True


# --- v2: справочники и планы -------------------------------------------------

class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    specialty_code: str = ""
    year_of_study: int = 1


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    specialty_code: Optional[str] = None
    year_of_study: Optional[int] = None


class DisciplineCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    total_hours: int = 0


class DisciplineUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    total_hours: Optional[int] = None


class PlanCreate(BaseModel):
    title: Optional[str] = None          # если пусто — сгенерируем из предмета и группы
    discipline_id: int
    group_id: int
    semester: int = 1
    academic_year: str = "2026/2027"


class PlanUpdate(BaseModel):
    title: Optional[str] = None
    semester: Optional[int] = None
    academic_year: Optional[str] = None


class TopicCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    hours: int = 0
    order: Optional[int] = None         # по умолчанию — в конец плана


class TopicUpdate(BaseModel):
    title: Optional[str] = None
    hours: Optional[int] = None
    order: Optional[int] = None


class LessonCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    lesson_type: str = "Лекция"
    scheduled_date: Optional[date_type] = None
    homework_desc: str = ""


class LessonUpdate(BaseModel):
    title: Optional[str] = None
    lesson_type: Optional[str] = None
    scheduled_date: Optional[date_type] = None
    homework_desc: Optional[str] = None


# ---------------------------------------------------------------------------
# Фоновая задача генерации
# ---------------------------------------------------------------------------

async def run_generation(lesson_id: int, extra_prompt: str, to_email_override: Optional[str] = None):
    """
    Полный пайплайн в фоне: генерация -> сохранение версии -> статус ready -> email.
    Использует собственные сессии БД (не зависит от запроса).

    v2: в контекст промпта добавляются предмет и группа (профиль специальности),
    чтобы модель адаптировала материал под конкретную аудиторию.
    """
    from database import engine

    import time as _time

    t_start = _time.monotonic()
    log.info("▶ Генерация урока %s запущена (фон)", lesson_id)
    db = Session(engine)
    try:
        lesson = db.get(KtpLesson, lesson_id)
        if lesson is None:
            log.error("Урок %s не найден (фон)", lesson_id)
            return

        topic = lesson.topic
        plan = topic.plan if topic else None
        discipline = plan.discipline if plan else None
        group = plan.group if plan else None

        # Контекст для LLM: предмет + группа/специальность
        ctx = dict(
            discipline_title=discipline.title if discipline else "",
            group_name=group.name if group else "",
            group_specialty=group.specialty_code if group else "",
            plan_title=plan.title if plan else "",
        )
        log.debug(
            "Урок %s: тема «%s», тип «%s», раздел «%s», предмет «%s», группа «%s», уточнение преподавателя: %s",
            lesson_id, lesson.title, lesson.lesson_type,
            topic.title if topic else "", ctx["discipline_title"], ctx["group_name"],
            extra_prompt.strip() or "(нет)",
        )

        try:
            content = await llm_service.generate_material(
                lesson_title=lesson.title,
                lesson_type=lesson.lesson_type,
                topic_title=topic.title if topic else "",
                extra_prompt=extra_prompt,
                **ctx,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка генерации урока %s (статус → error)", lesson_id)
            lesson.status = "error"
            lesson.last_error = f"{type(exc).__name__}: {exc}"
            db.commit()
            return

        # Сохраняем новую версию материала
        last_version = (
            db.query(MaterialVersion)
            .filter(MaterialVersion.lesson_id == lesson_id)
            .order_by(MaterialVersion.version_num.desc())
            .first()
        )
        next_num = (last_version.version_num + 1) if last_version else 1
        db.add(
            MaterialVersion(
                lesson_id=lesson_id,
                version_num=next_num,
                prompt_used=extra_prompt or "(стандартный промпт методиста)",
                raw_markdown_content=content,
            )
        )
        lesson.status = "ready"
        lesson.last_error = None
        db.commit()
        log.info(
            "✔ Урок %s: материал v%s сохранён (%d симв.), статус → ready, всего %.1f с",
            lesson_id, next_num, len(content), _time.monotonic() - t_start,
        )

        # Email (ошибки почты не должны ломать результат)
        settings = db.query(AppSettings).first()
        email = (to_email_override or "").strip() or (
            settings.email_for_notifications if settings and settings.notify_email_enabled else ""
        )
        if email:
            result = await send_completion_email(email, lesson.title, content)
            log.info("Урок %s: email-уведомление → %s", lesson_id, result)

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Фоновая задача сборки презентации PowerPoint
# ---------------------------------------------------------------------------

async def run_presentation_generation(lesson_id: int):
    """
    Фон: Markdown-конспект → JSON слайдов (LLM) → сборка .pptx (python-pptx) → статус ready.
    Статусы presentation_status: idle | generating | ready | error.
    """
    from database import engine

    import time as _time

    t_start = _time.monotonic()
    log.info("▶ Генерация презентации урока %s запущена (фон)", lesson_id)
    db = Session(engine)
    try:
        lesson = db.get(KtpLesson, lesson_id)
        if lesson is None:
            log.error("Урок %s не найден (фон, презентация)", lesson_id)
            return

        latest = (
            db.query(MaterialVersion)
            .filter(MaterialVersion.lesson_id == lesson_id)
            .order_by(MaterialVersion.version_num.desc())
            .first()
        )
        if latest is None or not (latest.raw_markdown_content or "").strip():
            lesson.presentation_status = "error"
            lesson.presentation_error = "Нет сгенерированного материала — сначала создайте конспект урока"
            db.commit()
            log.warning("Урок %s: презентации нет, материал отсутствует", lesson_id)
            return

        topic = lesson.topic
        plan = topic.plan if topic else None
        discipline = plan.discipline if plan else None
        group = plan.group if plan else None

        # 1. LLM: конспект → строгий JSON структуры слайдов
        try:
            slides_json = await pptx_service.extract_presentation_json(
                lesson_title=lesson.title,
                lesson_text=latest.raw_markdown_content,
                discipline_title=discipline.title if discipline else "",
                group_name=group.name if group else "",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка генерации JSON слайдов для урока %s", lesson_id)
            lesson.presentation_status = "error"
            lesson.presentation_error = f"{type(exc).__name__}: {exc}"
            db.commit()
            return

        # 2. python-pptx: сборка файла на диске
        out_path = _presentation_path(lesson_id)
        try:
            abs_path = pptx_service.build_pptx_file(slides_json, out_path)
            n_slides = pptx_service.validate_pptx(abs_path)  # контроль целостности собранного файла
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка сборки .pptx для урока %s", lesson_id)
            lesson.presentation_status = "error"
            lesson.presentation_error = f"{type(exc).__name__}: {exc}"
            db.commit()
            return

        lesson.presentation_path = abs_path
        lesson.presentation_status = "ready"
        lesson.presentation_error = None
        db.commit()
        log.info(
            "✔ Урок %s: презентация собрана (%d слайдов, %s), статус → ready, всего %.1f с",
            lesson_id, n_slides, os.path.basename(abs_path), _time.monotonic() - t_start,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Сериализаторы
# ---------------------------------------------------------------------------

def _lesson_brief(l: KtpLesson) -> dict:
    return {
        "id": l.id,
        "lesson_order": l.lesson_order,
        "title": l.title,
        "lesson_type": l.lesson_type,
        "scheduled_date": l.scheduled_date.isoformat() if l.scheduled_date else None,
        "homework_desc": l.homework_desc or "",
        "status": l.status,
        "last_error": l.last_error,
        "presentation_status": l.presentation_status or "idle",
        "has_material": len(l.versions) > 0,
        "versions_count": len(l.versions),
    }


def _plan_summary(p: KTPPlan) -> dict:
    lessons = [l for t in p.topics for l in t.lessons]
    return {
        "id": p.id,
        "title": p.title,
        "semester": p.semester,
        "academic_year": p.academic_year,
        "discipline_id": p.discipline_id,
        "discipline_title": p.discipline.title if p.discipline else "",
        "group_id": p.group_id,
        "group_name": p.group.name if p.group else "",
        "group_specialty": p.group.specialty_code if p.group else "",
        "topics_count": len(p.topics),
        "lessons_count": len(lessons),
        "ready_count": sum(1 for l in lessons if l.status == "ready"),
    }


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "mock_mode": llm_service.MOCK_MODE,
            "model_name": llm_service.MODEL_NAME,
            "smtp_on": smtp_configured(),
        },
    )


# ---------------------- Справочник: Группы ----------------------------------

@app.get("/api/groups")
def list_groups(db: Session = Depends(get_db)):
    groups = db.query(Group).order_by(Group.name).all()
    return {
        "groups": [
            {
                "id": g.id,
                "name": g.name,
                "specialty_code": g.specialty_code,
                "year_of_study": g.year_of_study,
            }
            for g in groups
        ]
    }


@app.post("/api/groups", status_code=201)
def create_group(body: GroupCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Название группы обязательно")
    if db.query(Group).filter_by(name=name).first():
        raise HTTPException(409, f"Группа «{name}» уже существует")
    g = Group(
        name=name,
        specialty_code=body.specialty_code.strip(),
        year_of_study=max(1, min(5, body.year_of_study)),
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return {"id": g.id, "name": g.name, "specialty_code": g.specialty_code, "year_of_study": g.year_of_study}


@app.put("/api/groups/{group_id}")
def update_group(group_id: int, body: GroupUpdate, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404, "Группа не найдена")
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(422, "Название группы не может быть пустым")
        clash = db.query(Group).filter(Group.name == new_name, Group.id != group_id).first()
        if clash:
            raise HTTPException(409, f"Группа «{new_name}» уже существует")
        g.name = new_name
    if body.specialty_code is not None:
        g.specialty_code = body.specialty_code.strip()
    if body.year_of_study is not None:
        g.year_of_study = max(1, min(5, body.year_of_study))
    db.commit()
    return {"ok": True, "id": g.id}


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)):
    g = db.get(Group, group_id)
    if g is None:
        raise HTTPException(404, "Группа не найдена")
    if g.plans:
        raise HTTPException(409, "Нельзя удалить: к группе привязаны КТП")
    db.delete(g)
    db.commit()
    return {"ok": True}


# ---------------------- Справочник: Предметы --------------------------------

@app.get("/api/disciplines")
def list_disciplines(db: Session = Depends(get_db)):
    ds = db.query(Discipline).order_by(Discipline.title).all()
    return {
        "disciplines": [
            {
                "id": d.id,
                "title": d.title,
                "description": d.description,
                "total_hours": d.total_hours,
            }
            for d in ds
        ]
    }


@app.post("/api/disciplines", status_code=201)
def create_discipline(body: DisciplineCreate, db: Session = Depends(get_db)):
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Название предмета обязательно")
    if db.query(Discipline).filter_by(title=title).first():
        raise HTTPException(409, f"Предмет «{title}» уже существует")
    d = Discipline(title=title, description=body.description.strip(), total_hours=max(0, body.total_hours))
    db.add(d)
    db.commit()
    db.refresh(d)
    return {"id": d.id, "title": d.title, "description": d.description, "total_hours": d.total_hours}


@app.put("/api/disciplines/{disc_id}")
def update_discipline(disc_id: int, body: DisciplineUpdate, db: Session = Depends(get_db)):
    d = db.get(Discipline, disc_id)
    if d is None:
        raise HTTPException(404, "Предмет не найден")
    if body.title is not None:
        t = body.title.strip()
        if not t:
            raise HTTPException(422, "Название предмета не может быть пустым")
        clash = db.query(Discipline).filter(Discipline.title == t, Discipline.id != disc_id).first()
        if clash:
            raise HTTPException(409, f"Предмет «{t}» уже существует")
        d.title = t
    if body.description is not None:
        d.description = body.description.strip()
    if body.total_hours is not None:
        d.total_hours = max(0, body.total_hours)
    db.commit()
    return {"ok": True, "id": d.id}


@app.delete("/api/disciplines/{disc_id}")
def delete_discipline(disc_id: int, db: Session = Depends(get_db)):
    d = db.get(Discipline, disc_id)
    if d is None:
        raise HTTPException(404, "Предмет не найден")
    if d.plans:
        raise HTTPException(409, "Нельзя удалить: к предмету привязаны КТП")
    db.delete(d)
    db.commit()
    return {"ok": True}


# ---------------------- КТП: планы ------------------------------------------

@app.get("/api/plans")
def list_plans(db: Session = Depends(get_db)):
    plans = (
        db.query(KTPPlan)
        .options(*[])  # отношения дочитываются лениво — для MVP достаточно
        .order_by(KTPPlan.id.desc())
        .all()
    )
    return {"plans": [_plan_summary(p) for p in plans]}


@app.post("/api/plans", status_code=201)
def create_plan(body: PlanCreate, db: Session = Depends(get_db)):
    discipline = db.get(Discipline, body.discipline_id)
    if discipline is None:
        raise HTTPException(404, "Предмет не найден")
    group = db.get(Group, body.group_id)
    if group is None:
        raise HTTPException(404, "Группа не найдена")

    title = (body.title or "").strip() or f"КТП: {discipline.title} для группы {group.name} ({body.academic_year})"
    plan = KTPPlan(
        title=title,
        discipline_id=discipline.id,
        group_id=group.id,
        semester=max(1, min(12, body.semester)),
        academic_year=(body.academic_year or "2026/2027").strip(),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _plan_summary(plan)


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(KTPPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "КТП не найден")
    topics_out = []
    for t in sorted(plan.topics, key=lambda x: (x.order or 0, x.id)):
        topics_out.append(
            {
                "id": t.id,
                "title": t.title,
                "hours": t.hours,
                "order": t.order,
                "lessons": [_lesson_brief(l) for l in sorted(t.lessons, key=lambda x: (x.lesson_order or 0, x.id))],
            }
        )
    out = _plan_summary(plan)
    out["topics"] = topics_out
    return out


@app.put("/api/plans/{plan_id}")
def update_plan(plan_id: int, body: PlanUpdate, db: Session = Depends(get_db)):
    plan = db.get(KTPPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "КТП не найден")
    if body.title is not None and body.title.strip():
        plan.title = body.title.strip()
    if body.semester is not None:
        plan.semester = max(1, min(12, body.semester))
    if body.academic_year is not None and body.academic_year.strip():
        plan.academic_year = body.academic_year.strip()
    db.commit()
    return {"ok": True, "id": plan.id}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(KTPPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "КТП не найден")
    db.delete(plan)  # каскадно удалятся темы и пары (cascade="all, delete-orphan")
    db.commit()
    return {"ok": True}


# ---------------------- КТП: темы (разделы) ---------------------------------

@app.post("/api/plans/{plan_id}/topics", status_code=201)
def create_topic(plan_id: int, body: TopicCreate, db: Session = Depends(get_db)):
    plan = db.get(KTPPlan, plan_id)
    if plan is None:
        raise HTTPException(404, "КТП не найден")
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Название темы обязательно")
    order = body.order
    if order is None:
        max_order = db.query(RpdTopic).filter(RpdTopic.ktp_plan_id == plan_id).count()
        order = max_order + 1
    t = RpdTopic(ktp_plan_id=plan_id, title=title, hours=max(0, body.hours), order=order)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "title": t.title, "hours": t.hours, "order": t.order, "lessons": []}


@app.put("/api/topics/{topic_id}")
def update_topic(topic_id: int, body: TopicUpdate, db: Session = Depends(get_db)):
    t = db.get(RpdTopic, topic_id)
    if t is None:
        raise HTTPException(404, "Тема не найдена")
    if body.title is not None and body.title.strip():
        t.title = body.title.strip()
    if body.hours is not None:
        t.hours = max(0, body.hours)
    if body.order is not None:
        t.order = body.order
    db.commit()
    return {"ok": True, "id": t.id}


@app.delete("/api/topics/{topic_id}")
def delete_topic(topic_id: int, db: Session = Depends(get_db)):
    t = db.get(RpdTopic, topic_id)
    if t is None:
        raise HTTPException(404, "Тема не найдена")
    db.delete(t)  # пары удаляются каскадно
    db.commit()
    return {"ok": True}


# ---------------------- КТП: пары (занятия) ---------------------------------

@app.post("/api/topics/{topic_id}/lessons", status_code=201)
def create_lesson(topic_id: int, body: LessonCreate, db: Session = Depends(get_db)):
    topic = db.get(RpdTopic, topic_id)
    if topic is None:
        raise HTTPException(404, "Тема не найдена")
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Название занятия обязательно")
    lesson_type = body.lesson_type.strip() or "Лекция"
    existing_types = {t[0] for t in db.query(KtpLesson.lesson_type).distinct().all()}
    if lesson_type not in LESSON_TYPES and lesson_type not in existing_types:
        # разрешаем и новые типы, но реестр известных — из LESSON_TYPES
        pass
    max_order = db.query(KtpLesson).filter(KtpLesson.topic_id == topic_id).count()
    l = KtpLesson(
        topic_id=topic_id,
        lesson_order=max_order + 1,
        title=title,
        lesson_type=lesson_type,
        scheduled_date=body.scheduled_date,
        homework_desc=body.homework_desc.strip(),
        status="idle",
    )
    db.add(l)
    db.commit()
    db.refresh(l)
    return _lesson_brief(l)


@app.put("/api/lessons/{lesson_id}")
def update_lesson(lesson_id: int, body: LessonUpdate, db: Session = Depends(get_db)):
    l = db.get(KtpLesson, lesson_id)
    if l is None:
        raise HTTPException(404, "Пара не найдена")
    if body.title is not None and body.title.strip():
        l.title = body.title.strip()
    if body.lesson_type is not None and body.lesson_type.strip():
        l.lesson_type = body.lesson_type.strip()
    if body.scheduled_date is not None:
        l.scheduled_date = body.scheduled_date
    if body.homework_desc is not None:
        l.homework_desc = body.homework_desc.strip()
    db.commit()
    return {"ok": True, "id": l.id, **_lesson_brief(l)}


@app.delete("/api/lessons/{lesson_id}")
def delete_lesson(lesson_id: int, db: Session = Depends(get_db)):
    l = db.get(KtpLesson, lesson_id)
    if l is None:
        raise HTTPException(404, "Пара не найдена")
    if l.status == "generating":
        raise HTTPException(409, "Нельзя удалить пару во время генерации")
    db.delete(l)  # версии материалов удаляются каскадно
    db.commit()
    return {"ok": True}


# ---------------------- Compat: прежнее дерево --------------------------------

@app.get("/api/ktp")
def get_ktp(db: Session = Depends(get_db)):
    """Совместимость с v1: все КТП-планы как единое дерево «курсов»."""
    plans = db.query(KTPPlan).order_by(KTPPlan.id).all()
    result = []
    for p in plans:
        topics_out = []
        for t in sorted(p.topics, key=lambda x: (x.order or 0, x.id)):
            topics_out.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "hours": t.hours,
                    "lessons": [_lesson_brief(l) for l in sorted(t.lessons, key=lambda x: (x.lesson_order or 0, x.id))],
                }
            )
        result.append(
            {
                "id": p.id,
                "title": p.title,
                "description": p.discipline.title if p.discipline else "",
                "topics": topics_out,
            }
        )
    return {"courses": result, "plans": [_plan_summary(p) for p in plans]}


@app.get("/api/meta/lesson_types")
def lesson_types():
    return {"lesson_types": list(LESSON_TYPES)}


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: int, db: Session = Depends(get_db)):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    latest = (
        db.query(MaterialVersion)
        .filter(MaterialVersion.lesson_id == lesson_id)
        .order_by(MaterialVersion.version_num.desc())
        .first()
    )
    versions_count = (
        db.query(MaterialVersion).filter(MaterialVersion.lesson_id == lesson_id).count()
    )
    topic = lesson.topic
    plan = topic.plan if topic else None
    discipline = plan.discipline if plan else None
    group = plan.group if plan else None
    return {
        "id": lesson.id,
        "title": lesson.title,
        "lesson_type": lesson.lesson_type,
        "lesson_order": lesson.lesson_order,
        "scheduled_date": lesson.scheduled_date.isoformat() if lesson.scheduled_date else None,
        "homework_desc": lesson.homework_desc or "",
        "status": lesson.status,
        "last_error": lesson.last_error,
        "presentation_status": lesson.presentation_status or "idle",
        "presentation_error": lesson.presentation_error,
        "presentation_ready": bool(
            lesson.presentation_status == "ready"
            and lesson.presentation_path
            and os.path.isfile(lesson.presentation_path)
        ),
        "topic": {"id": lesson.topic_id, "title": topic.title if topic else ""},
        "plan": {
            "id": plan.id if plan else None,
            "title": plan.title if plan else "",
            "discipline_title": discipline.title if discipline else "",
            "group_name": group.name if group else "",
            "group_specialty": group.specialty_code if group else "",
        },
        "latest_version": (
            {
                "version_num": latest.version_num,
                "prompt_used": latest.prompt_used,
                "raw_markdown_content": latest.raw_markdown_content,
                "created_at": latest.created_at.isoformat() if latest.created_at else None,
            }
            if latest
            else None
        ),
        # Промпт последней генерации — для автоподстановки в поле ввода на фронтенде.
        # Пустая строка, если материалов ещё нет или промпт не сохранялся (старые версии).
        "last_prompt": (latest.prompt_used or "") if latest else "",
        "versions_count": versions_count,
    }


@app.post("/api/lessons/{lesson_id}/generate")
def start_generate(
    lesson_id: int,
    body: GenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    if lesson.status == "generating":
        return {"status": "generating", "detail": "Генерация уже запущена"}

    lesson.status = "generating"
    lesson.last_error = None
    db.commit()
    user_prompt = body.effective_prompt
    log.info(
        "POST /api/lessons/%s/generate — статус → generating, фоновая задача поставлена (уточнение: %s)",
        lesson_id, user_prompt[:120] or "(нет)",
    )

    background_tasks.add_task(run_generation, lesson_id, user_prompt, body.email)
    return {"status": "generating"}


@app.get("/api/lessons/{lesson_id}/status")
def lesson_status(lesson_id: int, db: Session = Depends(get_db)):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    return {"id": lesson.id, "status": lesson.status, "title": lesson.title, "last_error": lesson.last_error}


# ---------------------- Презентация PowerPoint (.pptx) ----------------------

@app.post("/api/lessons/{lesson_id}/presentation/generate")
def start_presentation_generate(lesson_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Запуск сборки презентации: конспект → JSON слайдов (LLM) → .pptx (python-pptx) в фоне."""
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    has_material = (
        db.query(MaterialVersion).filter(MaterialVersion.lesson_id == lesson_id).count() > 0
    )
    if not has_material:
        raise HTTPException(422, "Сначала сгенерируйте материал урока — презентация строится по конспекту")
    if lesson.status == "generating":
        raise HTTPException(409, "Материал ещё генерируется — дождитесь готовности конспекта")
    if lesson.presentation_status == "generating":
        return {"presentation_status": "generating", "detail": "Генерация презентации уже запущена"}

    lesson.presentation_status = "generating"
    lesson.presentation_error = None
    db.commit()
    log.info("POST /api/lessons/%s/presentation/generate — presentation_status → generating", lesson_id)

    background_tasks.add_task(run_presentation_generation, lesson_id)
    return {"presentation_status": "generating"}


@app.get("/api/lessons/{lesson_id}/presentation/status")
def presentation_status(lesson_id: int, db: Session = Depends(get_db)):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    st = lesson.presentation_status or "idle"
    file_ok = bool(lesson.presentation_path and os.path.isfile(lesson.presentation_path))
    if st == "ready" and not file_ok:
        st = "error"  # файл потерялся на диске — считаем презентацию недоступной
    return {
        "id": lesson.id,
        "title": lesson.title,
        "presentation_status": st,
        "presentation_error": lesson.presentation_error,
        "download_url": f"/api/lessons/{lesson_id}/presentation/download" if st == "ready" else None,
    }


@app.get("/api/lessons/{lesson_id}/presentation/download")
def presentation_download(lesson_id: int, db: Session = Depends(get_db)):
    """Отдаёт собранный .pptx с заголовком Content-Disposition: attachment."""
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    path = lesson.presentation_path or _presentation_path(lesson_id)
    if lesson.presentation_status != "ready" or not os.path.isfile(path):
        raise HTTPException(409, "Презентация ещё не готова — сначала нажмите «Сгенерировать слайды»")

    safe_title = re.sub(r"[^\w\s\-–—()А-Яа-яЁё.]", "", lesson.title).strip() or f"lesson_{lesson_id}"
    filename = f"Презентация_{safe_title[:80]}.pptx"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=filename,
        content_disposition_type="attachment",
    )


@app.post("/api/lessons/{lesson_id}/save")
def save_manual(lesson_id: int, body: SaveRequest, db: Session = Depends(get_db)):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    if not body.content.strip():
        raise HTTPException(422, "Пустой контент сохранять нельзя")

    last_version = (
        db.query(MaterialVersion)
        .filter(MaterialVersion.lesson_id == lesson_id)
        .order_by(MaterialVersion.version_num.desc())
        .first()
    )
    next_num = (last_version.version_num + 1) if last_version else 1
    db.add(
        MaterialVersion(
            lesson_id=lesson_id,
            version_num=next_num,
            prompt_used="(ручное редактирование преподавателем)",
            raw_markdown_content=body.content,
        )
    )
    lesson.status = "ready"
    lesson.last_error = None
    db.commit()
    return {"status": "ready", "version_num": next_num}


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    s = db.query(AppSettings).first()
    return {
        "email_for_notifications": s.email_for_notifications if s else "",
        "notify_email_enabled": bool(s.notify_email_enabled) if s else False,
        "smtp_configured": smtp_configured(),
        "mock_mode": llm_service.MOCK_MODE,
        "model_name": llm_service.MODEL_NAME,
    }


@app.post("/api/settings")
def update_settings(body: SettingsRequest, db: Session = Depends(get_db)):
    s = db.query(AppSettings).first()
    if s is None:
        s = AppSettings()
        db.add(s)
    s.email_for_notifications = body.email_for_notifications.strip()
    s.notify_email_enabled = 1 if body.notify_email_enabled else 0
    db.commit()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
