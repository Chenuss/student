---
key: pptx.user
type: user
version: 1
description: User-промпт сборки слайдов: тема + полный Markdown-конспект
updated_at: 2026-10-25T04:37:15Z
placeholders: ctx_line, lesson_title, lesson_text
---
{% if ctx_line %}Контекст: {{ ctx_line }}.

{% endif %}Тема лекции: {{ lesson_title }}

Текст лекции (Markdown-конспект):

{{ lesson_text }}

Составь структуру презентации строго по правилам системного промпта. Ответ — только валидный JSON без markdown-обёртки.
