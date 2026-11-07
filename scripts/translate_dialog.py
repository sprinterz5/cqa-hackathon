"""Make a test copy of a dialog in another language (e.g. a Kazakh voice call from a Russian one).

Client / operator / bot lines are translated by the primary LLM; timestamps and speakers are kept;
known system events get their wording in the target language. The audio link is dropped (it would not match).

Usage: python scripts/translate_dialog.py DEV p4-ru-002 --lang KZ --group KZTEST
       python scripts/translate_dialog.py DEV p4-ru-002 --lang KZ --mixed 12   # client starts in Kazakh, then Russian
Writes data/<group>/<group>_raw.json (same format as the organizers' API) for upload / evaluation.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cqa.evaluator import parse_json  # noqa: E402
from cqa.llm import LLMClient, LLMError, providers_from_env  # noqa: E402

LANG_NAMES = {"KZ": "казахский", "EN": "английский", "RU": "русский"}
# Russian Dynamics events -> Kazakh wording (assumed; one real example in DEV: «Жазу және транскрипция басталды.»)
KZ_EVENTS = {
    "Агент присоединился к разговору с клиентом.": "Агент клиентпен сөйлесуге қосылды.",
    "Клиент завершил разговор.": "Клиент сөйлесуді аяқтады.",
    "Запись и транскрибирование запущены.": "Жазу және транскрипция басталды.",
    "Запись и транскрибирование приостановлены.": "Жазу және транскрипция тоқтатылды.",
    "Запись и транскрибирование возобновлены.": "Жазу және транскрипция жалғастырылды.",
    "Клиент находится на удержании.": "Клиент ұстауда.",
    "Клиент больше не находится на удержании.": "Клиент енді ұстауда емес.",
    "Инициирована консультация с агентом.": "Агентпен кеңес басталды.",
    "Началась консультация с агентом.": "Агентпен кеңес басталды.",
    "Агент вышел из разговора.": "Агент сөйлесуден шықты.",
}

PROMPT = """Переведи реплики телефонного разговора службы поддержки Freedom Broker на {lang} язык.
Сохрани смысл, вежливость, ошибки и недочёты оператора (они нужны для проверки оценщика качества), маски вида
«[Имя скрыто]» и «[Персональные данные скрыты]» оставь как есть. Это расшифровка устной речи: пиши разговорно.
Верни только JSON: {{"lines": ["перевод 1", "перевод 2", ...]}} — ровно {n} строк в том же порядке.

{lines}"""


async def translate(client: LLMClient, texts: list[str], lang: str) -> list[str]:
    """Translate a batch; on repeated failure split it in halves (gpt-oss sometimes returns empty answers)."""
    try:
        return await _translate(client, texts, lang)
    except (RuntimeError, LLMError):
        if len(texts) == 1:
            return texts  # keep the original line rather than fail the whole dialog
        mid = len(texts) // 2
        return await translate(client, texts[:mid], lang) + await translate(client, texts[mid:], lang)


async def _translate(client: LLMClient, texts: list[str], lang: str) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    msgs = [{"role": "user", "content": PROMPT.format(lang=LANG_NAMES.get(lang, lang), n=len(texts), lines=numbered)}]
    for _ in range(3):
        resp = await client.chat(msgs, temperature=0.2, max_tokens=8000)
        try:
            out = parse_json(resp.content)["lines"]
            if len(out) == len(texts):
                return [str(x) for x in out]
        except (ValueError, KeyError, TypeError):
            pass
    raise RuntimeError("модель не вернула перевод нужной длины")


async def main(a):
    raw = json.loads((ROOT / "data" / a.src_group / f"{a.src_group}_raw.json").read_text(encoding="utf-8"))
    rec = next(r for r in raw["data"]["records"] if r["conversation_id"] == a.conversation_id)
    tr = [dict(m) for m in rec["transcript"]]
    idx = [i for i, m in enumerate(tr) if m["speaker"] != "system"]
    if a.mixed:  # code-switching test: the first N client/operator lines in the target language, the rest unchanged
        idx = [i for i in idx if tr[i]["speaker"] != "bot"][:a.mixed]
    async with LLMClient([providers_from_env()["primary"]]) as client:
        for start in range(0, len(idx), a.batch):
            part = idx[start:start + a.batch]
            for i, t in zip(part, await translate(client, [tr[i]["text"] for i in part], a.lang)):
                tr[i]["text"] = t
            print(f"translated {min(start + a.batch, len(idx))}/{len(idx)}")
    if a.lang == "KZ":
        for m in tr:
            if m["speaker"] == "system":
                m["text"] = KZ_EVENTS.get(m["text"], m["text"])
    suffix = a.lang.lower() + ("mix" if a.mixed else "")
    new = {**rec, "conversation_id": rec["conversation_id"].replace("-ru-", f"-{suffix}-") + "-tr",
           "language": rec["language"] if a.mixed else a.lang, "transcript": tr, "audio_url": None}
    out = ROOT / "data" / a.group / f"{a.group}_raw.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else \
        {"error": "", "isSuccess": True, "data": {"dataset_group_id": a.group, "total_records": 0, "records": []}}
    recs = [r for r in data["data"]["records"] if r["conversation_id"] != new["conversation_id"]] + [new]
    data["data"].update(records=recs, total_records=len(recs))
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{new['conversation_id']} -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src_group")
    ap.add_argument("conversation_id")
    ap.add_argument("--lang", default="KZ")
    ap.add_argument("--group", default="KZTEST")
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--mixed", type=int, default=0, help="translate only the first N client/operator lines")
    asyncio.run(main(ap.parse_args()))
