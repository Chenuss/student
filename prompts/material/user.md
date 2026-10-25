---
key: material.user
type: user
version: 1
description: User-промпт генерации дидактического материала (контекст пары/предмета/группы)
updated_at: 2026-10-25T04:37:15Z
placeholders: discipline_title, group_name, group_specialty, plan_title, topic_title, lesson_title, lesson_type, extra_prompt
---
Дисциплина: {{ discipline_title | электротехника и электроника }} (СПО, колледж).
{% if group_name %}Целевая группа: {{ group_name }}{% if group_specialty %}, код специальности {{ group_specialty }}{% endif %}. Адаптируй пример, терминологию и производственные ситуации под профиль этой группы.
{% endif %}{% if plan_title %}КТП: {{ plan_title }}
{% endif %}Раздел КТП: {{ topic_title }}
Тема пары: {{ lesson_title }}
Тип занятия: {{ lesson_type }}
{% if extra_prompt %}Уточнение преподавателя (обязательно учти): {{ extra_prompt }}
{% endif %}Сгенерируй полный дидактический материал к этой паре.
