# ⚡ Генератор дидактических материалов и КТП по электротехнике

MVP веб-приложения для преподавателя колледжа (СПО): генерация конспектов пар
через **ProxyAPI.ru** в **фоновых задачах FastAPI** с **Web Notifications**,
email-копиями через SMTP и live-рендерингом **Markdown + KaTeX + Mermaid**.

## Возможности
- **Справочники**: учебные группы (название, код специальности, курс) и предметы (дисциплины) — полный CRUD.
- **Конструктор КТП**: создание нескольких планов «Предмет × Группа × семестр», добавление/редактирование/удаление тем-разделов и пар (Лекция / Практика / Лабораторная / Комбинированный урок), дата занятия, домашнее задание.
- Дерево активного КТП в левой панели со статусами пар: `idle / generating / ready / error`.
- Запуск генерации в `BackgroundTasks` — интерфейс не блокируется, можно свернуть браузер.
- Polling статуса раз в 3 сек → системное уведомление «Материалы для пары: […] успешно сгенерированы!».
- В промпт LLM передаются название предмета, группа и её специальность — материалы адаптируются под специфику группы.
- Опциональная отправка письма с конспектом (aiosmtplib; сбой почты не ломает сохранение).
- Версионирование материалов: каждая генерация/ручное сохранение = новая `MaterialVersion`.
- Редактор Markdown + рендер формул ($…$, $$…$$) и схем цепей (```mermaid```).
- **Без API-ключа работает MOCK-режим**: красивая заглушка про закон Ома с LaTeX и Mermaid.
- Миграция v1→v2: старые данные (курс → темы → пары) автоматически переносятся в новый КТП при первом запуске.

## Быстрый запуск

```bash
# 1. Зависимости (Python 3.10+)
pip install -r requirements.txt

# 2. Настройки
cp .env.example .env
#   .env можно НЕ редактировать — без PROXYAPI_KEY включится мок-режим.
#   Для боевой генерации: PROXYAPI_KEY=sk-...  MODEL_NAME=deepseek/deepseek-chat
#   Для писем:           SMTP_ENABLED=true, SMTP_HOST/PORT/USER/PASSWORD

# 3. Старт (БД ktp.db создаётся и сеидится автоматически при первом запуске)
uvicorn main:app --host 0.0.0.0 --port 8000
#   или: python main.py
```

Откройте **http://localhost:8000**, разрешите уведомления в браузере,
выберите пару слева → «🚀 Сгенерировать материалы».

> Web Notifications требуют `https` или `localhost` — при доступе по IP в
> локальной сети браузер может не дать разрешение (в этом случае работают
> всплывающие toast-уведомления в интерфейсе).

## API
| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `/api/groups` | список / создание учебной группы |
| PUT/DELETE | `/api/groups/{id}` | редактирование / удаление группы |
| GET/POST | `/api/disciplines` | список / создание предмета |
| PUT/DELETE | `/api/disciplines/{id}` | редактирование / удаление предмета |
| GET/POST | `/api/plans` | список КТП (с предметом и группой) / новый КТП |
| GET/PUT/DELETE | `/api/plans/{id}` | структура конкретного КТП / правки / удаление |
| POST | `/api/plans/{id}/topics` | добавить тему-раздел |
| PUT/DELETE | `/api/topics/{id}` | редактировать / удалить тему |
| POST | `/api/topics/{id}/lessons` | добавить пару (тип, дата) |
| PUT/DELETE | `/api/lessons/{id}` | редактировать / удалить пару |
| GET | `/api/lessons/{id}` | пара + последняя версия материала |
| POST | `/api/lessons/{id}/generate` | фоновый запуск генерации (`{prompt, email}`) |
| GET | `/api/lessons/{id}/status` | polling статуса |
| POST | `/api/lessons/{id}/save` | ручное сохранение (новая версия) |
| GET | `/api/ktp` | compat v1: все КТП как единое дерево |
| GET/POST | `/api/settings` | email преподавателя для рассылок |

## Структура проекта
```
main.py             # FastAPI: справочники, конструктор КТП, BackgroundTasks
database.py         # SQLAlchemy-модели v2 (Group, Discipline, KTPPlan, RpdTopic, KtpLesson, MaterialVersion), миграция v1→v2, seed
llm_service.py      # AsyncOpenAI (ProxyAPI.ru) + системный промпт методиста + mock-fallback
notifier.py         # send_completion_email (aiosmtplib, изолированные ошибки)
templates/index.html# SPA: Tailwind + Alpine.js + селектор КТП, модалка «+ Новый КТП», inline-редактирование, marked + KaTeX + Mermaid + Notifications
.env.example        # PROXYAPI_*, MODEL_NAME, SMTP_*, TEACHER_EMAIL, DATABASE_URL
```
