"""
main.py — FastAPI приложение «Генератор дидактических материалов и КТП».

Эндпоинты:
  GET  /                      — SPA (templates/index.html)
  GET  /api/ktp               — дерево курса со статусами пар
  GET  /api/lessons/{id}      — данные пары + последняя версия материала
  POST /api/lessons/{id}/generate — запуск генерации в BackgroundTasks
  GET  /api/lessons/{id}/status   — polling статуса пары
  POST /api/lessons/{id}/save     — ручное сохранение edited-текста (новая версия)
  GET/POST /api/settings      — email преподавателя для уведомлений
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.requests import Request

import llm_service
from database import AppSettings, KtpLesson, MaterialVersion, init_db
from notifier import send_completion_email, smtp_configured

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("ktp-app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


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
    email: Optional[str] = None  # куда продублировать готовый материал


class SaveRequest(BaseModel):
    content: str


class SettingsRequest(BaseModel):
    email_for_notifications: str = ""
    notify_email_enabled: bool = True


# ---------------------------------------------------------------------------
# Фоновая задача генерации
# ---------------------------------------------------------------------------

async def run_generation(lesson_id: int, extra_prompt: str, to_email_override: Optional[str] = None):
    """
    Полной пайплайн в фоне: генерация -> сохранение версии -> статус ready -> email.
    Использует собственные сессии БД (не зависит от запроса).
    """
    from database import engine

    db = Session(engine)
    try:
        lesson = db.get(KtpLesson, lesson_id)
        if lesson is None:
            log.error("Урок %s не найден (фон)", lesson_id)
            return

        topic = lesson.topic
        try:
            content = await llm_service.generate_material(
                lesson_title=lesson.title,
                lesson_type=lesson.lesson_type,
                topic_title=topic.title if topic else "",
                extra_prompt=extra_prompt,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка генерации урока %s", lesson_id)
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
        log.info("Урок %s: материал v%s сохранён, статус → ready", lesson_id, next_num)

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


@app.get("/api/ktp")
def get_ktp(db: Session = Depends(get_db)):
    """Дерево: курсы → темы → пары со статусами."""
    from database import Course

    courses = db.query(Course).all()
    result = []
    for c in courses:
        topics_out = []
        for t in c.topics:
            lessons_out = [
                {
                    "id": l.id,
                    "lesson_order": l.lesson_order,
                    "title": l.title,
                    "lesson_type": l.lesson_type,
                    "status": l.status,
                    "has_material": any(True for _ in l.versions),
                }
                for l in t.lessons
            ]
            topics_out.append(
                {"id": t.id, "title": t.title, "hours": t.hours, "lessons": lessons_out}
            )
        result.append(
            {"id": c.id, "title": c.title, "description": c.description, "topics": topics_out}
        )
    return {"courses": result}


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
    return {
        "id": lesson.id,
        "title": lesson.title,
        "lesson_type": lesson.lesson_type,
        "lesson_order": lesson.lesson_order,
        "status": lesson.status,
        "last_error": lesson.last_error,
        "topic": {"id": lesson.topic_id, "title": lesson.topic.title if lesson.topic else ""},
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

    background_tasks.add_task(run_generation, lesson_id, body.prompt, body.email)
    return {"status": "generating"}


@app.get("/api/lessons/{lesson_id}/status")
def lesson_status(lesson_id: int, db: Session = Depends(get_db)):
    lesson = db.get(KtpLesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "Пара не найдена")
    return {"id": lesson.id, "status": lesson.status, "title": lesson.title, "last_error": lesson.last_error}


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
