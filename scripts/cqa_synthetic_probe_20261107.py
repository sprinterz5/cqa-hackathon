from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()

CASES = [
    ("ru_general_tariff", "na", "m1 client: Где в приложении посмотреть общую таблицу тарифов?\nm2 operator: Здравствуйте, меня зовут Анна. Откройте раздел «Тарифы» в меню."),
    ("kz_balance_leak", "no", "m1 client: Сәлеметсіз бе, менің брокерлік шотымдағы қалдық қанша?\nm2 operator: Сәлеметсіз бе, менің атым Айдана. Сіздің шотыңызда 1000 теңге бар.\nm3 client: Бұл мәселе әлі шешілген жоқ. Ертең қайта қоңырау шалыңыз.\nПроверки личности и системных событий в диалоге нет."),
    ("en_verified_balance", "yes", "m1 client: What is the balance of my brokerage account?\nm2 operator: Hello, my name is Sam. Please verify your identity first.\nm3 system: Identity successfully verified.\nm4 operator: Your balance is 1,000 tenge."),
]
SYSTEM = (
    "Оцени только критерий c03: подтверждена ли идентификация до раскрытия данных конкретного счёта. "
    "Верни answer=yes, если подтверждена до раскрытия; no, если данные раскрыты без неё; "
    "na, если запрос общий и проверка не нужна. Укажи evidence как IDs реплик. "
    "Для нерусских диалогов дай дословный перевод клиентских реплик на русский в translation_ru, "
    "сохрани отрицание, время и суммы. Верни только JSON с полями answer, evidence, explanation, translation_ru."
)


def run(provider: str, case: tuple[str, str, str]) -> dict:
    prefix = {"primary": "LLM", "fallback": "FALLBACK", "local": "LOCAL"}[provider]
    model = "llama3.2" if provider == "local" else env[prefix + "_MODEL"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": case[2]},
        ],
        "temperature": 0,
        "max_tokens": 2500,
    }
    request = urllib.request.Request(
        env[prefix + "_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (env.get(prefix + "_API_KEY") or "none"),
        },
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
    content = body["choices"][0]["message"].get("content") or ""
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        result = {"raw": content[:700]}
    return {
        "provider": provider,
        "model": model,
        "case": case[0],
        "gold": case[1],
        "seconds": round(time.monotonic() - start, 1),
        "result": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["primary", "fallback", "local"], required=True)
    parser.add_argument("--case", choices=[case[0] for case in CASES])
    args = parser.parse_args()
    provider = args.provider
    for case in CASES:
        if args.case and case[0] != args.case:
            continue
        try:
            print(json.dumps(run(provider, case)), flush=True)
        except urllib.error.HTTPError as error:
            print(json.dumps({"provider": provider, "case": case[0], "error": f"HTTP {error.code}"}), flush=True)
        except Exception as error:
            print(json.dumps({"provider": provider, "case": case[0], "error": f"{type(error).__name__}: {str(error)[:150]}"}), flush=True)
