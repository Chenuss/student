"""
database.py (v2) — SQLAlchemy-модели многопредметной КТП + pre-seed базы данных.

Структура:
  Group           — учебная группа (Э-21, specialty_code 13.02.11, курс)
  Discipline      — предмет/дисциплина (Электротехника, Монтаж сетей и т.п.)
  KTPPlan         — Календарно-тематический план (предмет × группа × семестр)
  RpdTopic        — раздел/тема РПД, привязана к плану (ktp_plan_id), есть order
  KtpLesson       — пара КТП: тип, дата, домашнее задание, статус генерации
  MaterialVersion — версии сгенерированного/отредактированного материала
  AppSettings     — настройки приложения (email преподавателя и т.п.)

Миграция v1→v2: если в старой БД ещё существует таблица courses (схема v1),
данные автоматически переносятся в новую структуру (Discipline + KTPPlan).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
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

# Статус сборки презентации PowerPoint — та же шкала состояний, что и у материалов
PRESENTATION_STATUSES = LESSON_STATUSES

LESSON_TYPES = (
    "Лекция",
    "Практика",
    "Лабораторная",
    "Комбинированный урок",
    "Контрольная работа",
)


class Group(Base):
    """Учебная группа: Э-21, ИС-32 и т.п."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    specialty_code: Mapped[str] = mapped_column(String(32), default="")
    year_of_study: Mapped[int] = mapped_column(Integer, default=1)

    plans: Mapped[list["KTPPlan"]] = relationship(back_populates="group")


class Discipline(Base):
    """Предмет (дисциплина): Электротехника, Монтаж сетей и т.п."""

    __tablename__ = "disciplines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    total_hours: Mapped[int] = mapped_column(Integer, default=0)

    plans: Mapped[list["KTPPlan"]] = relationship(back_populates="discipline")


class KTPPlan(Base):
    """Сам календарно-тематический план: предмет × группа × семестр."""

    __tablename__ = "ktp_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    discipline_id: Mapped[int] = mapped_column(ForeignKey("disciplines.id"), nullable=False)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    semester: Mapped[int] = mapped_column(Integer, default=1)
    academic_year: Mapped[str] = mapped_column(String(20), default="2026/2027")

    discipline: Mapped["Discipline"] = relationship(back_populates="plans")
    group: Mapped["Group"] = relationship(back_populates="plans")
    topics: Mapped[list["RpdTopic"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="RpdTopic.order"
    )


class RpdTopic(Base):
    """Раздел/тема РПД внутри конкретного КТП."""

    __tablename__ = "rpd_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ktp_plan_id: Mapped[int] = mapped_column(ForeignKey("ktp_plans.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    hours: Mapped[int] = mapped_column(Integer, default=0)
    order: Mapped[int] = mapped_column(Integer, default=1)

    plan: Mapped["KTPPlan"] = relationship(back_populates="topics")
    lessons: Mapped[list["KtpLesson"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan", order_by="KtpLesson.lesson_order"
    )


class KtpLesson(Base):
    """Пара КТП (урок): тип занятия, дата, ДЗ, статус генерации материалов."""

    __tablename__ = "ktp_lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("rpd_topics.id"), nullable=False)
    lesson_order: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lesson_type: Mapped[str] = mapped_column(String(100), default="Лекция")
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    homework_desc: Mapped[str] = mapped_column(Text, default="")
    # idle | generating | ready | error
    status: Mapped[str] = mapped_column(String(20), default="idle")
    last_error: Mapped[str] = mapped_column(Text, nullable=True)
    # --- презентация PowerPoint (.pptx): статус сборки и путь к файлу на диске ---
    # idle | generating | ready | error
    presentation_status: Mapped[str] = mapped_column(String(20), default="idle")
    presentation_path: Mapped[str] = mapped_column(String(512), default="")
    presentation_error: Mapped[str] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    topic: Mapped["RpdTopic"] = relationship(back_populates="lessons")
    versions: Mapped[list["MaterialVersion"]] = relationship(
        back_populates="lesson",
        cascade="all, delete-orphan",
        order_by="MaterialVersion.version_num",
    )


class MaterialVersion(Base):
    """Версии материала пары — без изменений относительно v1."""

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
# Pre-seed: справочники + пример КТП «Электротехника и электроника» для Э-21
# ---------------------------------------------------------------------------

SEED_GROUPS = [
    {"name": "Э-21", "specialty_code": "13.02.11", "year_of_study": 2},
    {"name": "ИС-32", "specialty_code": "09.02.07", "year_of_study": 3},
    {"name": "МС-11", "specialty_code": "08.02.09", "year_of_study": 1},
]

SEED_DISCIPLINES = [
    {
        "title": "Электротехника и электроника",
        "description": (
            "Базовый общетехнический курс СПО. Специальности: 09.02.07, 13.02.11, 15.02.16."
        ),
        "total_hours": 48,
    },
    {
        "title": "Монтаж и техническая эксплуатация электрических сетей",
        "description": "Профессиональный модуль ПМ.02: электромонтажные работы, чтение схем, ПУЭ.",
        "total_hours": 64,
    },
]

# Тема: (заголовок, часы, [(пара, тип, дз), ...])
SEED_PLAN_ELECTRO = {
    "title_template": "КТП: {disc} для группы {group} ({year})",
    "semester": 3,
    "academic_year": "2026/2027",
    "discipline_idx": 0,
    "group_name": "Э-21",
    "topics": [
        (
            "Раздел 1. Основы теории электрических цепей",
            18,
            [
                ("Основные понятия и величины электрических цепей", "Лекция", ""),
                ("Закон Ома для участка цепи и полной цепи", "Комбинированный урок",
                 "§ 12, задачи 12.3–12.6"),
                ("Законы Кирхгофа. Расчёт разветвлённых цепей", "Практика", ""),
                ("Цепи переменного тока. Активные и реактивные элементы", "Лекция", ""),
            ],
        ),
        (
            "Раздел 2. Электронные устройства и преобразователи",
            16,
            [
                ("Полупроводниковые диоды и выпрямители", "Лабораторная", ""),
                ("Биполярные и полевые транзисторы. Каскады усиления", "Лекция", ""),
                ("Цифровая электроника. Логические элементы и триггеры", "Практика", ""),
            ],
        ),
        (
            "Раздел 3. Электрические машины и приводы",
            14,
            [
                ("Трансформаторы: устройство, принцип действия, расчёт", "Комбинированный урок", ""),
                ("Асинхронные двигатели. Механическая характеристика", "Лекция", ""),
                ("Контроль и защита электродвигателей", "Практика", ""),
            ],
        ),
    ],
}

SEED_PLAN_NETWORKS = {
    "title_template": "КТП: {disc} для группы {group} ({year})",
    "semester": 4,
    "academic_year": "2026/2027",
    "discipline_idx": 1,
    "group_name": "МС-11",
    "topics": [
        (
            "Раздел 1. Чтение электрических схем и планов",
            16,
            [
                ("Условные обозначения на электрических схемах (ЕСКД/ПУЭ)", "Лекция", ""),
                ("Принципиальные и монтажные схемы освещения квартиры", "Практика", ""),
            ],
        ),
        (
            "Раздел 2. Электромонтажные работы",
            24,
            [
                ("Прокладка кабелей и проводов в лотках и гофре", "Лабораторная", ""),
                ("Монтаж распределительных щитов, автоматов и УЗО", "Практика",
                 "Подготовить однолинейную схему щита"),
            ],
        ),
    ],
}


def _migrate_v1_to_v2(conn) -> None:
    """Переносим данные из схемы v1 (courses → rpd_topics.course_id) в v2."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    tables = set(insp.get_table_names())
    if "courses" not in tables or "rpd_topics" not in tables:
        return

    topic_cols = {c["name"] for c in insp.get_columns("rpd_topics")}
    if "ktp_plan_id" in topic_cols and "course_id" not in topic_cols:
        return  # уже мигрировано

    lesson_cols = {c["name"] for c in insp.get_columns("ktp_lessons")}
    # Добавляем недостающие колонки v2
    if "scheduled_date" not in lesson_cols:
        conn.execute(text("ALTER TABLE ktp_lessons ADD COLUMN scheduled_date DATE"))
    if "homework_desc" not in lesson_cols:
        conn.execute(text("ALTER TABLE ktp_lessons ADD COLUMN homework_desc TEXT DEFAULT ''"))

    rows = conn.execute(text("SELECT id, title, description FROM courses")).fetchall()
    for cid, ctitle, cdesc in rows:
        res = conn.execute(
            text("INSERT INTO disciplines (title, description, total_hours) VALUES (:t, :d, 0)"),
            {"t": ctitle, "d": cdesc or ""},
        )
        disc_id = res.lastrowid
        res = conn.execute(
            text("INSERT INTO groups (name, specialty_code, year_of_study) VALUES (:n, '', 1)"),
            {"n": f"Мигрировано из курса #{cid}"},
        )
        grp_id = res.lastrowid
        res = conn.execute(
            text(
                "INSERT INTO ktp_plans (title, discipline_id, group_id, semester, academic_year)"
                " VALUES (:t, :d, :g, 1, '2026/2027')"
            ),
            {"t": f"Импортированный КТП: {ctitle}", "d": disc_id, "g": grp_id},
        )
        plan_id = res.lastrowid
        trows = conn.execute(
            text("SELECT id, title, hours FROM rpd_topics WHERE course_id = :cid ORDER BY id"),
            {"cid": cid},
        ).fetchall()
        for i, (tid, ttitle, thours) in enumerate(trows, start=1):
            conn.execute(
                text("UPDATE rpd_topics SET ktp_plan_id = :p, \"order\" = :o WHERE id = :tid"),
                {"p": plan_id, "o": i, "tid": tid},
            )
    # Удаляем старую связь: пересоздаём rpd_topics без course_id нельзя простым UPDATE,
    # но SQLite позволяет оставить лишнюю nullable-колонку — помечаем её удалённой через
    # DROP в новой таблице и копированием (безопасный путь):
    conn.execute(text("DROP TABLE IF EXISTS rpd_topics_old"))
    conn.execute(text("ALTER TABLE rpd_topics RENAME TO rpd_topics_old"))
    conn.execute(
        text(
            "CREATE TABLE rpd_topics ("
            " id INTEGER NOT NULL PRIMARY KEY,"
            " ktp_plan_id INTEGER NOT NULL REFERENCES ktp_plans (id),"
            " title VARCHAR(255) NOT NULL,"
            " hours INTEGER DEFAULT 0,"
            ' "order" INTEGER DEFAULT 1)'
        )
    )
    conn.execute(
        text(
            "INSERT INTO rpd_topics (id, ktp_plan_id, title, hours, \"order\")"
            " SELECT id, ktp_plan_id, title, hours, \"order\" FROM rpd_topics_old"
            " WHERE ktp_plan_id IS NOT NULL"
        )
    )
    orphan_ids = [r[0] for r in conn.execute(
        text("SELECT id FROM rpd_topics_old WHERE ktp_plan_id IS NULL")
    ).fetchall()]
    conn.execute(text("DROP TABLE rpd_topics_old"))
    if orphan_ids:
        marks = ",".join(str(i) for i in orphan_ids)
        conn.execute(text(f"DELETE FROM ktp_lessons WHERE topic_id IN ({marks})"))
    conn.execute(text("DROP TABLE IF EXISTS courses"))


def _migrate_presentation_columns(conn) -> None:
    """SQLite: добавляем колонки презентации в ktp_lessons, если их ещё нет (мягкая миграция)."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    if "ktp_lessons" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("ktp_lessons")}
    if "presentation_status" not in cols:
        conn.execute(text("ALTER TABLE ktp_lessons ADD COLUMN presentation_status VARCHAR(20) DEFAULT 'idle'"))
    if "presentation_path" not in cols:
        conn.execute(text("ALTER TABLE ktp_lessons ADD COLUMN presentation_path VARCHAR(512) DEFAULT ''"))
    if "presentation_error" not in cols:
        conn.execute(text("ALTER TABLE ktp_lessons ADD COLUMN presentation_error TEXT"))


def _migrate_prompt_used_column(conn) -> None:
    """SQLite: добавляем колонку prompt_used в material_versions, если её ещё нет.

    Мягкая миграция для существующих БД: старые строки получают '' (DEFAULT),
    поэтому инициализация SQLite не ломается при наличии записей.
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(conn)
    if "material_versions" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("material_versions")}
    if "prompt_used" not in cols:
        conn.execute(text("ALTER TABLE material_versions ADD COLUMN prompt_used TEXT DEFAULT ''"))


def init_db() -> None:
    """Создаёт таблицы, выполняет миграцию v1→v2 и seed при первом запуске."""
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        try:
            _migrate_v1_to_v2(conn)
        except Exception:  # noqa: BLE001 — миграция best-effort, не ломаем старт
            import logging

            logging.getLogger("ktp-db").exception("Миграция v1→v2 не выполнена")
        try:
            _migrate_presentation_columns(conn)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("ktp-db").exception("Миграция колонок презентации не выполнена")
        try:
            _migrate_prompt_used_column(conn)
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("ktp-db").exception("Миграция колонки prompt_used не выполнена")

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        # --- Seed групп ---
        if session.query(Group).count() == 0:
            for g in SEED_GROUPS:
                session.add(Group(**g))

        # --- Seed предметов ---
        if session.query(Discipline).count() == 0:
            for d in SEED_DISCIPLINES:
                session.add(Discipline(**d))

        session.flush()

        # --- Seed КТП, если планов ещё нет ---
        if session.query(KTPPlan).count() == 0:
            for spec in (SEED_PLAN_ELECTRO, SEED_PLAN_NETWORKS):
                discipline = (
                    session.query(Discipline)
                    .order_by(Discipline.id)
                    .offset(spec["discipline_idx"])
                    .first()
                )
                group = session.query(Group).filter_by(name=spec["group_name"]).first()
                if discipline is None or group is None:
                    continue
                plan = KTPPlan(
                    title=spec["title_template"].format(
                        disc=discipline.title, group=group.name, year=spec["academic_year"]
                    ),
                    discipline_id=discipline.id,
                    group_id=group.id,
                    semester=spec["semester"],
                    academic_year=spec["academic_year"],
                )
                session.add(plan)
                session.flush()

                for t_idx, (t_title, t_hours, lessons) in enumerate(spec["topics"], start=1):
                    topic = RpdTopic(
                        ktp_plan_id=plan.id, title=t_title, hours=t_hours, order=t_idx
                    )
                    session.add(topic)
                    session.flush()
                    for l_idx, l_data in enumerate(lessons, start=1):
                        title, ltype = l_data[0], l_data[1]
                        hw = l_data[2] if len(l_data) > 2 else ""
                        session.add(
                            KtpLesson(
                                topic_id=topic.id,
                                lesson_order=l_idx,
                                title=title,
                                lesson_type=ltype,
                                homework_desc=hw,
                                status="idle",
                            )
                        )
            session.commit()

        # --- Seed настроек ---
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
    print("База данных v2 инициализирована и заполнена (seed).")
