"""Application service: datasets, evaluation jobs with per-criterion cache, appeals, decisions, metrics."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx

from .checklist import Checklist
from .evaluator import CriterionEvaluation, Evaluator, score_evaluations
from .export import build_results, validate_results
from .facts import extract_facts, facts_to_prompt
from .llm import LLMClient, Provider, load_env, providers_from_env
from .prompts import PROMPT_VERSION
from .scoring import ScoringError, Verdict, score, validate_verdict
from .store import Store
from .stt import LANG_CODES, STTConfig, assign_roles, guess_roles, merge_segments, stt_from_env, transcribe
from .transcript import SPEAKERS, normalize, parse_plain_text

ROOT = Path(__file__).resolve().parent.parent
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
               ".webm": "audio/webm", ".flac": "audio/flac"}
MAX_AUDIO_BYTES = 100 * 1024 * 1024


class ServiceError(ValueError):
    pass


def cache_key(checklist: Checklist, criterion_id: str, record: dict, models: list[str], samples: int) -> str:
    c = checklist.criterion(criterion_id)
    payload = {
        "prompt": PROMPT_VERSION, "models": models, "samples": f"{samples}+adaptive2" if samples > 1 else samples,
        "criterion": c.model_dump(mode="json", exclude={"enabled"}),
        "rules": checklist.general_rules, "context": checklist.company_context or "",
        "channel": record.get("channel"), "transcript": record.get("transcript"),
        # facts are part of the prompt: if their computation changes, results must not come from the cache
        "facts": facts_to_prompt(extract_facts(normalize(record))),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Service:
    def __init__(self, store: Store, providers: dict[str, Provider] | None = None,
                 default_samples: int = 3, client_factory=None, stt: STTConfig | None = None,
                 transcriber=None, audio_dir: Path | None = None):
        self.store = store
        self.providers = providers if providers is not None else providers_from_env()
        self.default_samples = default_samples
        self.client_factory = client_factory or (lambda chain: LLMClient(chain))
        self.stt = stt if stt is not None or providers is not None else stt_from_env()
        self.transcriber = transcriber or transcribe
        self.audio_dir = audio_dir or ROOT / "data" / "audio"
        self._local_audio_lock = asyncio.Lock()
        self.jobs: dict[str, dict] = {}

    # ---------- checklists ----------
    def seed_checklists(self, folder: Path = ROOT / "checklists") -> None:
        for f in sorted(folder.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            if not self.store.get_checklist(data["id"]):
                Checklist.model_validate(data)
                self.store.save_checklist(data)

    def checklist(self, cid: str, version: int | None = None) -> Checklist:
        data = self.store.get_checklist(cid, version)
        if not data:
            raise ServiceError(f"чек-лист {cid} v{version or 'latest'} не найден")
        return Checklist.model_validate(data)

    def save_checklist(self, data: dict) -> Checklist:
        cl = Checklist.model_validate(data)  # raises on invalid config
        version = self.store.save_checklist(cl.model_dump(mode="json"))
        return self.checklist(cl.id, version)

    # ---------- dialogs ----------
    async def fetch_group(self, group_id: str) -> int:
        load_env()
        base = os.environ.get("DATA_API_URL")
        if not base:
            raise ServiceError("DATA_API_URL не задан в .env")
        url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"group_id": group_id})
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.get(url)
        payload = r.json()
        if not payload.get("isSuccess"):
            raise ServiceError(f"API организаторов: {payload.get('error') or r.status_code}")
        records = payload["data"]["records"]
        for rec in records:
            self.store.upsert_dialog(rec, group_id, "api")
        return len(records)

    def upload(self, payload: dict | list | str, group_id: str = "UPLOAD", channel: str = "agent chat",
               language: str | None = None, conversation_id: str | None = None) -> list[str]:
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith("{") or text.startswith("["):
                payload = json.loads(text)
            else:
                cid = conversation_id or f"upload-{uuid.uuid4().hex[:6]}"
                payload = parse_plain_text(text, cid, channel, language)
        if isinstance(payload, dict) and "data" in payload and "records" in (payload["data"] or {}):
            payload = payload["data"]["records"]
        records = payload if isinstance(payload, list) else [payload]
        ids = []
        for rec in records:
            if not rec.get("transcript"):
                raise ServiceError("в диалоге нет реплик (transcript)")
            rec = {**rec, "conversation_id": rec.get("conversation_id") or conversation_id
                   or f"upload-{uuid.uuid4().hex[:6]}"}
            rec.setdefault("channel", channel)
            rec.setdefault("language", language)
            rec.setdefault("audio_url", None)
            self.store.upsert_dialog(rec, group_id, "upload")
            ids.append(rec["conversation_id"])
        return ids

    def dialog_view(self, conversation_id: str) -> dict:
        row = self.store.get_dialog(conversation_id)
        if not row:
            raise ServiceError(f"диалог {conversation_id} не найден")
        d = normalize(row["raw"])
        f = extract_facts(d)
        return {"group_id": row["group_id"], "dialog": d.model_dump(), "facts": f.model_dump(),
                "has_audio": bool(d.audio_url or row["raw"].get("audio_file")),
                "asr": row["raw"].get("asr")}

    async def audio_source(self, conversation_id: str) -> dict:
        """The organizers' audio_url is an HTML player page with a short-lived mp3 link: extract it.
        Uploaded recordings are served by the API itself (src is relative to the API base)."""
        row = self.store.get_dialog(conversation_id)
        if row and row["raw"].get("audio_file"):
            return {"src": f"dialogs/{urllib.parse.quote(conversation_id)}/audio/file", "page": None}
        url = row and row["raw"].get("audio_url")
        if not url:
            raise ServiceError("у диалога нет записи")
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            r = await http.get(url)
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("audio/"):
            return {"src": url, "page": url}
        m = re.search(r'<source[^>]+src="([^"]+)"', r.text) or re.search(r'<audio[^>]+src="([^"]+)"', r.text)
        if not m:
            raise ServiceError("не удалось найти ссылку на аудио на странице записи")
        return {"src": html.unescape(m.group(1)), "page": url}

    def audio_file(self, conversation_id: str) -> tuple[Path, str]:
        row = self.store.get_dialog(conversation_id)
        name = row and row["raw"].get("audio_file")
        path = self.audio_dir / name if name else None
        if not path or not path.exists():
            raise ServiceError("файл записи не найден")
        return path, AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")

    # ---------- uploaded call recordings: audio -> STT -> roles -> dialog ----------
    def start_audio_job(self, data: bytes, filename: str, language: str | None = None,
                        conversation_id: str | None = None, group_id: str = "UPLOAD", mode: str = "cloud",
                        replace: bool = False) -> str:
        """cloud: Alem STT (RU/EN: whole file with phrase timestamps; KZ: pause chunks to the Kazakh model).
        local: Whisper + voice groups + translation on this machine (cqa/audio_pipeline.py).
        Roles are always assigned by the LLM; in local mode it also sees the voice group of each phrase."""
        if mode not in ("cloud", "local"):
            raise ServiceError("режим: cloud или local")
        if mode == "cloud" and not self.stt:
            raise ServiceError("распознавание речи не настроено (STT_API_KEY в .env)")
        if mode == "local":
            from .audio_pipeline import modes
            if not modes()["local"]["available"]:
                raise ServiceError("локальный режим не установлен: см. requirements-audio.txt и scripts/install_audio.py")
        ext = Path(filename or "").suffix.lower()
        if ext not in AUDIO_TYPES:
            raise ServiceError(f"формат не поддерживается: {ext or 'без расширения'}; нужен "
                               + ", ".join(AUDIO_TYPES))
        if not data:
            raise ServiceError("пустой файл")
        if len(data) > MAX_AUDIO_BYTES:
            raise ServiceError("файл больше 100 МБ")
        language = (language or "").upper() or None
        if language and language not in LANG_CODES:
            raise ServiceError("язык: RU, KZ, EN или пусто (определить автоматически)")
        cid = (conversation_id or "").strip() or f"call-{uuid.uuid4().hex[:6]}"
        if not re.fullmatch(r"[\w.\-]{1,80}", cid):
            raise ServiceError("ID диалога: буквы, цифры, «-», «_», «.»")
        if not replace and self.store.get_dialog(cid):
            raise ServiceError(f"диалог {cid} уже есть — задайте другой ID")
        # roles: the cloud LLM, else the local one; without any LLM phrase cues are used (marked for review)
        chain = next(([self.providers[p]] + ([self.providers["fallback"]] if p == "primary" and "fallback" in self.providers else [])
                      for p in ("primary", "local") if p in self.providers), [])
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "kind": "audio", "mode": mode, "status": "running", "stage": "stt", "detail": None,
               "conversation_id": cid, "file": filename, "bytes": len(data), "started_at": time.time(),
               "finished_at": None, "total": 1, "done": 0, "failed": [], "results": {}}
        self.jobs[job_id] = job
        loop = asyncio.get_running_loop()

        async def recognize(path: Path) -> tuple[dict, list[str] | None, str]:
            """-> ({"segments", "language", "duration"}, voice group per phrase or None, STT model name)."""
            if mode == "local":
                from .audio_pipeline import process_audio

                def progress(stage: str):
                    loop.call_soon_threadsafe(job.update, {"detail": stage})
                async with self._local_audio_lock:  # CPU-heavy models: one recording at a time
                    rec = await asyncio.to_thread(process_audio, path, "local", language, progress)
                segs = [{k: s.get(k) for k in ("start", "end", "text", "source_language", "translation_ru")}
                        for s in rec["transcript"]]
                langs = [s["source_language"] for s in segs if s.get("source_language")]
                lang = language or (max(set(langs), key=langs.count) if langs else None)
                return ({"segments": segs, "language": lang, "duration": segs[-1]["end"] if segs else 0},
                        [s["speaker"] for s in rec["transcript"]], "local whisper large-v3 + ECAPA")
            if language == "KZ":
                from . import silence_audio
                if silence_audio.available():  # the Kazakh model has no phrase timestamps: cut by pauses first
                    from .audio_pipeline import AlemSTT
                    segs = await asyncio.to_thread(AlemSTT().transcribe, path, "KZ")
                    segs = [{"start": s["start"], "end": s["end"], "text": s["text"], "source_language": "KZ"}
                            for s in segs]
                    return ({"segments": segs, "language": "KZ", "duration": segs[-1]["end"] if segs else 0},
                            None, "speech-to-text-kk")
            return await self.transcriber(self.stt, data, filename, language), None, self.stt.model

        async def run():
            try:
                self.audio_dir.mkdir(parents=True, exist_ok=True)
                stored = f"{cid}{ext}"
                (self.audio_dir / stored).write_bytes(data)
                t0 = time.monotonic()
                asr, voices, stt_model = await recognize(self.audio_dir / stored)
                if not asr["segments"]:
                    raise ServiceError("в записи не распознано ни одной фразы")
                job["stt_s"] = round(time.monotonic() - t0, 1)
                job["stage"] = "roles"
                t0 = time.monotonic()
                if chain:
                    async with self.client_factory(chain) as client:
                        roles, role_source, role_error = await assign_roles(client, asr["segments"], voices)
                else:
                    roles, role_source, role_error = guess_roles(asr["segments"]), "context", "LLM не настроен"
                job["roles_s"] = round(time.monotonic() - t0, 1)
                lang = language or asr["language"]
                record = {
                    "conversation_id": cid, "channel": "voice call", "language": lang,
                    "audio_url": None, "audio_file": stored,
                    "transcript": merge_segments(asr["segments"], roles, role_source, voices),
                    "asr": {"mode": mode, "stt_model": stt_model,
                            "roles_model": chain[0].model if role_source == "ai" else None,
                            "voice_groups": len(set(voices)) if voices else None,
                            "role_source": role_source, "role_error": role_error, "language": lang,
                            "language_source": "user" if language else "detected", "file": filename,
                            "duration_s": asr["duration"], "segments": len(asr["segments"]),
                            "dropped_segments": asr.get("dropped", 0),
                            "stt_s": job["stt_s"], "roles_s": job["roles_s"]},
                }
                self.store.upsert_dialog(record, group_id, "audio")
                job.update(stage="done", status="done", done=1, language=lang, role_source=role_source,
                           lines=len(record["transcript"]), duration_s=asr["duration"])
            except Exception as e:
                job.update(status="error", error=str(e)[:300])
            finally:
                job["finished_at"] = time.time()

        job["task"] = asyncio.get_running_loop().create_task(run())
        return job_id

    def set_speaker(self, conversation_id: str, index: int, speaker: str) -> dict:
        """A reviewer fixes the role of one transcript line (roles of uploaded recordings are AI guesses)."""
        row = self.store.get_dialog(conversation_id)
        if not row:
            raise ServiceError(f"диалог {conversation_id} не найден")
        if speaker not in SPEAKERS:
            raise ServiceError("роль: client, operator, bot или system")
        raw = json.loads(json.dumps(row["raw"]))
        lines = raw.get("transcript") or []
        if not 0 <= index < len(lines):
            raise ServiceError("нет такой реплики")
        lines[index]["speaker"] = speaker
        lines[index]["role_source"] = "reviewer"
        self.store.upsert_dialog(raw, row["group_id"], row.get("source") or "upload")
        return self.dialog_view(conversation_id)

    # ---------- evaluation ----------
    def _chain(self, provider: str) -> list[Provider]:
        if provider == "local":
            if "local" not in self.providers:
                raise ServiceError("локальная модель не настроена (LOCAL_BASE_URL / LOCAL_MODEL)")
            return [self.providers["local"]]
        chain = [self.providers[p] for p in ("primary", "fallback") if p in self.providers]
        if not chain:
            raise ServiceError("LLM не настроен (LLM_BASE_URL / LLM_MODEL в .env)")
        return chain

    async def evaluate_dialog(self, client: LLMClient, chain: list[Provider], conversation_id: str,
                              checklist: Checklist, samples: int, force: bool = False) -> dict:
        row = self.store.get_dialog(conversation_id)
        if not row:
            raise ServiceError(f"диалог {conversation_id} не найден")
        record = row["raw"]
        d = normalize(record)
        models = [f"{p.name}:{p.model}" for p in chain[:1]]
        keys = {c.id: cache_key(checklist, c.id, record, models, samples) for c in checklist.active_criteria}
        cached: dict[str, CriterionEvaluation] = {}
        if not force:
            for cid, k in keys.items():
                hit = self.store.cache_get(k)
                if hit and hit.get("status") == "ok":
                    cached[cid] = CriterionEvaluation.model_validate(hit)
        missing = [c.id for c in checklist.active_criteria if c.id not in cached]
        t0 = time.monotonic()
        calls, tokens, cost, used_models = [], 0, 0.0, []
        fresh: dict[str, CriterionEvaluation] = {}
        if missing:
            ev = Evaluator(client, samples=samples)
            res = await ev.evaluate(checklist, d, only=missing)
            for c in res.criteria:
                fresh[c.criterion_id] = c
                if c.status == "ok":
                    self.store.cache_put(keys[c.criterion_id], c.model_dump())
            calls, tokens, cost, used_models = res.calls, res.total_tokens, res.cost_usd, res.models
        merged = [cached.get(c.id) or fresh[c.id] for c in checklist.active_criteria]
        sc, err = score_evaluations(checklist, merged)
        result = {
            "conversation_id": d.conversation_id, "channel": d.channel,
            "checklist_id": checklist.id, "checklist_version": checklist.version,
            "checklist_name": checklist.name, "prompt_version": PROMPT_VERSION,
            "models": used_models or models, "samples": samples,
            "criteria": [c.model_dump() for c in merged],
            "cached_criteria": sorted(cached), "evaluated_criteria": missing,
            "score": sc.model_dump() if sc else None, "score_error": err,
            "calls": calls, "total_tokens": tokens, "cost_usd": cost,
            "elapsed_s": round(time.monotonic() - t0, 1),
        }
        result["id"] = self.store.add_evaluation(result)
        return result

    def start_job(self, conversation_ids: list[str], checklist_id: str, version: int | None = None,
                  samples: int | None = None, provider: str = "primary", force: bool = False) -> str:
        checklist = self.checklist(checklist_id, version)
        chain = self._chain(provider)
        if not conversation_ids:
            raise ServiceError("не выбраны диалоги")
        job_id = uuid.uuid4().hex[:10]
        job = {"id": job_id, "status": "running", "total": len(conversation_ids), "done": 0,
               "failed": [], "results": {}, "current": None, "checklist_id": checklist.id,
               "checklist_version": checklist.version, "provider": provider,
               "started_at": time.time(), "finished_at": None}
        self.jobs[job_id] = job

        async def run():
            try:
                async with self.client_factory(chain) as client:
                    for cid in conversation_ids:
                        job["current"] = cid
                        try:
                            r = await self.evaluate_dialog(client, chain, cid, checklist,
                                                           samples or self.default_samples, force)
                            job["results"][cid] = {"evaluation_id": r["id"],
                                                   "total": (r["score"] or {}).get("total"),
                                                   "error": r["score_error"]}
                        except Exception as e:  # keep the batch going
                            job["failed"].append({"conversation_id": cid, "error": str(e)[:300]})
                        job["done"] += 1
                job["status"] = "done"
            except Exception as e:
                job["status"] = "error"
                job["error"] = str(e)[:300]
            finally:
                job["current"] = None
                job["finished_at"] = time.time()

        job["task"] = asyncio.get_running_loop().create_task(run())
        return job_id

    def job_view(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ServiceError("задача не найдена")
        return {k: v for k, v in job.items() if k != "task"}

    # ---------- results, appeals, decisions ----------
    def evaluation_view(self, evaluation_id: int) -> dict:
        ev = self.store.get_evaluation(evaluation_id)
        if not ev:
            raise ServiceError("оценка не найдена")
        checklist = self.checklist(ev["checklist_id"], ev["checklist_version"])
        appeals = self.store.list_appeals(evaluation_id=evaluation_id)
        decisions = self.store.decisions_for_evaluation(evaluation_id)
        latest = {}
        for dcs in decisions:
            latest[dcs["criterion_id"]] = dcs
        final_verdicts, rows, pending = [], [], []
        for c in ev["criteria"]:
            dcs = latest.get(c["criterion_id"])
            ans, val = c["answer"], c["value"]
            if dcs and dcs["decision"] == "change":
                ans, val = dcs["answer"], dcs["value"]
            if ans is None:
                pending.append(c["criterion_id"])
            else:
                final_verdicts.append(Verdict(criterion_id=c["criterion_id"], answer=ans, value=val))
            rows.append({**c, "decision": dcs, "final_answer": ans, "final_value": val,
                         "appeals": [a for a in appeals if a["criterion_id"] == c["criterion_id"]]})
        final, final_err = None, None
        if pending:
            final_err = f"требуют ручной проверки: {', '.join(pending)}"
        else:
            try:
                final = score(checklist, final_verdicts).model_dump()
            except ScoringError as e:
                final_err = str(e)
        return {**ev, "criteria": rows, "checklist": checklist.model_dump(mode="json"),
                "final_score": final, "final_score_error": final_err,
                "changed": any(d["decision"] == "change" for d in decisions)}

    def add_appeal(self, evaluation_id: int, criterion_id: str, comment: str, author: str | None) -> int:
        ev = self.store.get_evaluation(evaluation_id)
        if not ev:
            raise ServiceError("оценка не найдена")
        if criterion_id not in {c["criterion_id"] for c in ev["criteria"]}:
            raise ServiceError(f"критерия {criterion_id} нет в этой оценке")
        if not comment.strip():
            raise ServiceError("добавьте комментарий к апелляции")
        if any(a["criterion_id"] == criterion_id and a["status"] == "open"
               for a in self.store.list_appeals(evaluation_id=evaluation_id)):
            raise ServiceError("по этому критерию уже есть открытая апелляция")
        return self.store.add_appeal(evaluation_id, criterion_id, comment.strip(), author)

    def decide(self, appeal_id: int, decision: str, reason: str, answer: str | None = None,
               value: float | None = None, reviewer: str | None = None) -> dict:
        ap = self.store.get_appeal(appeal_id)
        if not ap:
            raise ServiceError("апелляция не найдена")
        if ap["status"] != "open":
            raise ServiceError("апелляция уже рассмотрена")
        if decision not in ("confirm", "change"):
            raise ServiceError("decision: confirm или change")
        if not reason.strip():
            raise ServiceError("укажите причину решения")
        ev = self.store.get_evaluation(ap["evaluation_id"])
        if decision == "change":
            checklist = self.checklist(ev["checklist_id"], ev["checklist_version"])
            crit = checklist.criterion(ap["criterion_id"])
            try:
                validate_verdict(crit, Verdict(criterion_id=crit.id, answer=answer or "", value=value))
            except (ScoringError, ValueError) as e:
                raise ServiceError(f"недопустимое значение: {e}")
        else:
            answer, value = None, None
        self.store.add_decision(appeal_id, decision, answer, value, reason.strip(), reviewer)
        return self.evaluation_view(ap["evaluation_id"])

    def appeals_queue(self, status: str | None = "open") -> list[dict]:
        out = []
        for a in self.store.list_appeals(status=status):
            ev = self.store.get_evaluation(a["evaluation_id"])
            crit = next((c for c in ev["criteria"] if c["criterion_id"] == a["criterion_id"]), {})
            out.append({**a, "conversation_id": ev["conversation_id"], "checklist_id": ev["checklist_id"],
                        "ai_answer": crit.get("answer"), "ai_value": crit.get("value"),
                        "ai_explanation": crit.get("explanation"), "evidence": crit.get("evidence")})
        return out

    def quality(self) -> dict:
        """AI vs. reviewer agreement per criterion (reviewer decisions act as labels)."""
        stats: dict[str, dict] = {}
        for d in self.store.all_decisions():
            s = stats.setdefault(d["criterion_id"], {"reviewed": 0, "changed": 0})
            s["reviewed"] += 1
            s["changed"] += d["decision"] == "change"
        for s in stats.values():
            s["agreement"] = round(1 - s["changed"] / s["reviewed"], 3) if s["reviewed"] else None
        evals = self.store.latest_evaluations()
        conf: dict[str, list[float]] = {}
        dist: dict[str, dict[str, int]] = {}
        for ev in evals.values():
            for c in ev["criteria"]:
                conf.setdefault(c["criterion_id"], []).append(c.get("confidence") or 0)
                key = f"{c['value']:g}" if c["answer"] == "score" else str(c["answer"])
                dist.setdefault(c["criterion_id"], {}).setdefault(key, 0)
                dist[c["criterion_id"]][key] += 1
        return {"reviewer_agreement": stats,
                "mean_confidence": {k: round(sum(v) / len(v), 3) for k, v in conf.items() if v},
                "answer_distribution": dist, "evaluated_dialogs": len(evals)}

    # ---------- export in the organizers' results.json format ----------
    def export_results(self, group_id: str, checklist_id: str = "freedom_default", version: int | None = None,
                       team_name: str | None = None) -> dict:
        checklist = self.checklist(checklist_id, version)
        ids = [d["conversation_id"] for d in self.store.list_dialogs(group_id)]
        if not ids:
            raise ServiceError(f"в группе {group_id} нет диалогов")
        evals = {}
        for cid in ids:
            e = self.store.latest_evaluation(cid, checklist.id, checklist.version)
            if e:
                evals[cid] = e
        kinds = {name: ("local" if name == "local" else "external_api") for name in self.providers}
        data = build_results(checklist, ids, evals, team_name or os.environ.get("TEAM_NAME", "CQA team"), kinds)
        return {"results": data, "problems": validate_results(data, ids),
                "evaluated": len(evals), "total": len(ids), "checklist_version": checklist.version}

    # ---------- import results of scripts/evaluate.py ----------
    def import_cli_evals(self, group_id: str, checklist_id: str = "freedom_default") -> int:
        folder = ROOT / "data" / group_id / "evals"
        checklist = self.checklist(checklist_id, 1)
        chain = self._chain("primary")
        models = [f"{p.name}:{p.model}" for p in chain[:1]]
        n = 0
        for f in sorted(folder.glob("*.json")):
            res = json.loads(f.read_text(encoding="utf-8"))
            row = self.store.get_dialog(res["conversation_id"])
            if not row or res.get("checklist_id") != checklist.id:
                continue
            prev = self.store.latest_evaluation(res["conversation_id"], checklist.id)
            if prev and prev.get("imported_from") == f.name and prev.get("elapsed_s") == res.get("elapsed_s"):
                continue  # already imported
            samples = max((len(c["samples"]) for c in res["criteria"]), default=1)
            for c in res["criteria"]:
                if c["status"] == "ok":
                    self.store.cache_put(cache_key(checklist, c["criterion_id"], row["raw"], models, samples), c)
            sc, err = score_evaluations(checklist, [CriterionEvaluation.model_validate(c) for c in res["criteria"]])
            self.store.add_evaluation({
                **res, "checklist_version": checklist.version, "checklist_name": checklist.name,
                "samples": samples, "cached_criteria": [], "evaluated_criteria": [c["criterion_id"] for c in res["criteria"]],
                "score": sc.model_dump() if sc else None, "score_error": err, "imported_from": f.name,
            })
            n += 1
        return n
