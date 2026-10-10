# ⚡ Генератор дидактических материалов и КТП по электротехнике

MVP веб-приложения для преподавателя колледжа (СПО): генерация конспектов пар
через **ProxyAPI.ru** в **фоновых задачах FastAPI** с **Web Notifications**,
email-копиями через SMTP и live-рендерингом **Markdown + KaTeX + Mermaid**.

## Возможности
- Дерево КТП (курс → разделы → пары) со статусами: `idle / generating / ready / error`.
- Запуск генерации в `BackgroundTasks` — интерфейс не блокируется, можно свернуть браузер.
- Polling статуса раз в 3 сек → системное уведомление «Материалы для пары: […] успешно сгенерированы!».
- Опциональная отправка письма с конспектом (aiosmtplib; сбой почты не ломает сохранение).
- Версионирование материалов: каждая генерация/ручное сохранение = новая `MaterialVersion`.
- Редактор Markdown + рендер формул ($…$, $$…$$) и схем цепей (```mermaid```).
- **Без API-ключа работает MOCK-режим**: красивая заглушка про закон Ома с LaTeX и Mermaid.

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
| GET | `/api/ktp` | дерево курса со статусами пар |
| GET | `/api/lessons/{id}` | пара + последняя версия материала |
| POST | `/api/lessons/{id}/generate` | фоновый запуск генерации (`{prompt, email}`) |
| GET | `/api/lessons/{id}/status` | polling статуса |
| POST | `/api/lessons/{id}/save` | ручное сохранение (новая версия) |
| GET/POST | `/api/settings` | email преподавателя для рассылок |

## Структура проекта
```
main.py             # FastAPI: маршруты + BackgroundTasks
database.py         # SQLAlchemy-модели + pre-seed КТП по электротехнике
llm_service.py      # AsyncOpenAI (ProxyAPI.ru) + системный промпт методиста + mock-fallback
notifier.py         # send_completion_email (aiosmtplib, изолированные ошибки)
templates/index.html# SPA: Tailwind + Alpine.js + marked + KaTeX + Mermaid + Notifications
.env.example        # PROXYAPI_*, MODEL_NAME, SMTP_*, TEACHER_EMAIL, DATABASE_URL
```
