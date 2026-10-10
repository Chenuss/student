"""
database.py — SQLAlchemy-модели и pre-seed базы данных.

Структура:
  Course          — дисциплина (например «Электротехника и электроника»)
  RpdTopic        — раздел/тема РПД, привязана к курсу
  KtpLesson       — пара КТП (урок), хранит статус генерации
  MaterialVersion — версии сгенерированного/отредактированного материала
  AppSettings     — настройки приложения (email преподавателя и т.п.)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ktp.db")

# Для SQLite включаем check_same_thread=False — фоновые задачи работают в других потоках
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Модели
# ---------------------------------------------------------------------------

LESSON_STATUSES = ("idle", "generating", "ready", "error")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    topics: Mapped[list["RpdTopic"]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="RpdTopic.id"
    )


class RpdTopic(Base):
    __tablename__ = "rpd_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    hours: Mapped[int] = mapped_column(Integer, default=0)

    course: Mapped["Course"] = relationship(back_populates="topics")
    lessons: Mapped[list["KtpLesson"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", order_by="KtpLesson.lesson_order"
    )


class KtpLesson(Base):
    __tablename__ = "ktp_lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("rpd_topics.id"), nullable=False)
    lesson_order: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lesson_type: Mapped[str] = mapped_column(String(100), default="Комбинированный урок")
    # idle | generating | ready | error
    status: Mapped[str] = mapped_column(String(20), default="idle")
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    topic: Mapped["RpdTopic"] = relationship(back_populates="lessons")
    versions: Mapped[list["MaterialVersion"]] = relationship(
        back_populates="lesson",
        cascade="all, delete-orphan",
        order_by="MaterialVersion.version_num",
    )


class MaterialVersion(Base):
    __tablename__ = "material_versions"
    __table_args__ = (UniqueConstraint("lesson_id", "version_num", name="uq_lesson_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("ktp_lessons.id"), nullable=False)
    version_num: Mapped[int] = mapped_column(Integer, default=1)
    prompt_used: Mapped[str] = mapped_column(Text, default="")
    raw_markdown_content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    lesson: Mapped["KtpLesson"] = relationship(back_populates="versions")


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_for_notifications: Mapped[str] = mapped_column(String(255), default="")
    notify_email_enabled: Mapped[int] = mapped_column(Integer, default=1)  # 0/1


# ---------------------------------------------------------------------------
# Pre-seed: наполняем базу примером КТП по электротехнике при первом старте
# ---------------------------------------------------------------------------

SEED_DATA = [
    {
        "title": "Электротехника и электроника",
        "description": (
            "Базовый курс СПО по общетехническим дисциплинам. "
            "Специальности: 09.02.07, 13.02.11, 15.02.16."
        ),
        "topics": [
            {
                "title": "Раздел 1. Основы теории электрических цепей",
                "hours": 18,
                "lessons": [
                    ("Основные понятия и величины электрических цепей", "Вводная лекция"),
                    ("Закон Ома для участка цепи и полной цепи", "Комбинированный урок"),
                    ("Законы Кирхгофа. Расчёт разветвлённых цепей", "Урок практических работ"),
                    ("Электрические цепи переменного тока. Активные и реактивные элементы", "Лекция с элементами демонстрации"),
                ],
            },
            {
                "title": "Раздел 2. Электронные устройства и преобразователи",
                "hours": 16,
                "lessons": [
                    ("Полупроводниковые диоды и выпрямители", "Комбинированный урок"),
                    ("Биполярные и полевые транзисторы. Каскады усиления", "Лекция с расчётом схем"),
                    ("Цифровая электроника. Логические элементы и триггеры", "Урок практических работ"),
                ],
            },
            {
                "title": "Раздел 3. Электрические машины и приводы",
                "hours": 14,
                "lessons": [
                    ("Трансформаторы: устройство, принцип действия, расчёт", "Комбинированный урок"),
                    ("Асинхронные двигатели. Механическая характеристика", "Лекция"),
                    ("Контроль и защита электродвигателей", "Урок обобщения"),
                ],
            },
        ],
    }
]


def init_db() -> None:
    """Создаёт таблицы и выполняет seed при первом запуске."""
    Base.metadata.create_all(bind=engine)

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        # Seed курсов, если база пуста
        if session.query(Course).count() == 0:
            for c in SEED_DATA:
                course = Course(title=c["title"], description=c["description"])
                session.add(course)
                session.flush()  # получаем course.id

                for t in c["topics"]:
                    topic = RpdTopic(
                        course_id=course.id, title=t["title"], hours=t["hours"]
                    )
                    session.add(topic)
                    session.flush()

                    for idx, (lesson_title, lesson_type) in enumerate(t["lessons"], start=1):
                        session.add(
                            KtpLesson(
                                topic_id=topic.id,
                                lesson_order=idx,
                                title=lesson_title,
                                lesson_type=lesson_type,
                                status="idle",
                            )
                        )

        # Seed настроек
        if session.query(AppSettings).count() == 0:
            session.add(
                AppSettings(
                    email_for_notifications=os.getenv("TEACHER_EMAIL", ""),
                    notify_email_enabled=1,
                )
            )

        session.commit()


if __name__ == "__main__":
    init_db()
    print("База данных инициализирована и заполнена (seed).")
