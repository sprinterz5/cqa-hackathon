"""Prompt assembly. Everything criterion-specific comes from the checklist config."""
from __future__ import annotations

from .checklist import Checklist, Criterion, CriterionType
from .facts import Facts, facts_to_prompt, transcript_to_prompt
from .transcript import Dialog

PROMPT_VERSION = "v1"

SYSTEM = """Ты — эксперт службы контроля качества. Ты оцениваешь работу ОПЕРАТОРА в диалоге с клиентом по ОДНОМУ критерию чек-листа.

Как работать:
1. Внимательно прочитай критерий, условие N/A и шкалу. Применяй их буквально, ничего не додумывая.
2. Найди в транскрипте реплики, которые подтверждают или опровергают выполнение критерия. Ссылайся на них по ID (m1, m2, ...).
3. Диалог может быть на русском, казахском или английском — оценивай по одинаковым правилам. Объяснение всегда пиши на русском.
4. Ответь строго одним JSON-объектом без markdown и текста вокруг.

Общие правила чек-листа:
{rules}{context}"""

ANSWER_SPEC = {
    CriterionType.BINARY: '"yes" (Да) или "no" (Нет){na}; поле "value" = null',
    CriterionType.FLAG: '"yes" (нарушение выявлено) или "no" (не выявлено); поле "value" = null',
    CriterionType.SCALE: '"score"{na}; при "score" поле "value" — одна из ступеней шкалы: {steps}',
}

USER = """КРИТЕРИЙ: {title}
Тип ответа: {type_label}
Что оценивается: {description}
{na_block}{scale_block}{guidance_block}
ФАКТЫ О ДИАЛОГЕ (вычислены автоматически, используй как контекст):
{facts}

ТРАНСКРИПТ ({channel}):
{transcript}

Верни JSON:
{{
  "reasoning": "пошаговый разбор: что требует критерий, какие реплики это подтверждают или опровергают",
  "answer": {answer_spec},
  "value": <число или null>,
  "evidence": [{{"from": "mN", "to": "mN"}}],
  "explanation": "1–2 предложения на русском: ответ и почему; для пропущенного действия — где оно ожидалось; для N/A — какое условие выполнено"
}}
В "evidence" укажи реплики-основания: конкретные сообщения или участок, где ожидалось действие. Для N/A — реплики, показывающие условие неприменимости."""

TYPE_LABEL = {
    CriterionType.BINARY: "Да / Нет",
    CriterionType.SCALE: "шкала",
    CriterionType.FLAG: "флаг грубого нарушения (Да / Нет), без баллов",
}


def build_messages(checklist: Checklist, criterion: Criterion, dialog: Dialog, facts: Facts) -> list[dict]:
    c = criterion.for_channel(dialog.channel)
    rules = "\n".join(f"- {r}" for r in checklist.general_rules) or "- (нет)"
    context = (f"\n\nСправочная информация о компании и продуктах (используй для проверки корректности; "
               f"если вопроса здесь нет — не считай ответ ошибочным без явных признаков):\n{checklist.company_context}"
               if (checklist.company_context or "").strip() else "")
    na = ' или "na" (N/A)' if c.na_allowed else ""
    steps = ", ".join(str(int(s.value) if float(s.value).is_integer() else s.value)
                      for s in sorted(c.scale or [], key=lambda s: -s.value))
    answer_spec = ANSWER_SPEC[c.type].format(na=na, steps=steps)
    na_block = (f"N/A разрешён, только если: {c.na_condition}\n" if c.na_allowed
                else "N/A для этого критерия не допускается.\n")
    scale_block = ""
    if c.type == CriterionType.SCALE:
        scale_block = "Шкала (выбери ровно одну ступень):\n" + "\n".join(
            f"  {s.value:g} — {s.description}" for s in sorted(c.scale, key=lambda s: -s.value)) + "\n"
    guidance_block = f"Уточнения: {c.guidance}\n" if c.guidance else ""
    channel = "звонок, автоматическое распознавание речи" if dialog.is_call else "чат"
    return [
        {"role": "system", "content": SYSTEM.format(rules=rules, context=context)},
        {"role": "user", "content": USER.format(
            title=c.title, type_label=TYPE_LABEL[c.type], description=c.description,
            na_block=na_block, scale_block=scale_block, guidance_block=guidance_block,
            facts=facts_to_prompt(facts), channel=channel,
            transcript=transcript_to_prompt(dialog, facts), answer_spec=answer_spec,
        )},
    ]
