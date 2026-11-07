"""HTTP API + static frontend.

Run:  .venv/bin/uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from cqa.audio_pipeline import modes as stt_modes, process_audio
from cqa.call_standard import analyze_call
from cqa.service import Service, ServiceError
from cqa.store import Store
from cqa.transcript import normalize

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
UPLOADS = ROOT / "data" / "uploads"
MAX_AUDIO_BYTES = 50 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class FetchBody(BaseModel):
    group_id: str


class UploadBody(BaseModel):
    content: str | dict | list
    group_id: str = "UPLOAD"
    channel: str = "agent chat"
    language: str | None = None
    conversation_id: str | None = None


class EvalBody(BaseModel):
    conversation_ids: list[str]
    checklist_id: str
    checklist_version: int | None = None
    samples: int | None = None
    provider: str = "primary"
    force: bool = False


class CallStandardBody(BaseModel):
    provider: str = "primary"
    approved_external_api: bool = False


class AppealBody(BaseModel):
    criterion_id: str
    comment: str
    author: str | None = None


class DecisionBody(BaseModel):
    decision: str  # confirm | change
    reason: str
    answer: str | None = None
    value: float | None = None
    reviewer: str | None = None



def bootstrap(db_path: str | None = None, service: Service | None = None) -> Service:
    import_datasets = service is None
    if service is None:
        service = Service(Store(db_path or os.environ.get("CQA_DB", str(ROOT / "data" / "cqa.db"))))
    service.seed_checklists()
    if import_datasets and not service.store.list_dialogs():  # first start: import already downloaded datasets
        for raw in sorted((ROOT / "data").glob("*/*_raw.json")):
            group = raw.parent.name
            for rec in json.loads(raw.read_text(encoding="utf-8"))["data"]["records"]:
                service.store.upsert_dialog(rec, group, "api")
    return service


def create_app(service: Service | None = None) -> FastAPI:
    holder: dict[str, Service] = {}
    audio_jobs: dict[str, dict] = {}
    audio_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["svc"] = bootstrap(service=service)
        yield

    app = FastAPI(title="CQA — Conversation Quality Analytics", lifespan=lifespan)
    # The UI may be embedded elsewhere (e.g. a Dynamics 365 web resource) and call this API cross-origin.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def svc() -> Service:
        return holder["svc"]

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, e: ServiceError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(ValidationError)
    async def _validation_error(_: Request, e: ValidationError):
        return JSONResponse({"detail": "; ".join(err["msg"] for err in e.errors())}, status_code=422)

    # ---------- meta ----------
    @app.get("/api/health")
    def health():
        s = svc()
        return {"ok": True, "providers": {k: p.public() for k, p in s.providers.items()},
                "default_samples": s.default_samples, "stt": stt_modes()}

    # ---------- checklists ----------
    @app.get("/api/checklists")
    def list_checklists():
        return svc().store.list_checklists()

    @app.get("/api/checklists/{cid}")
    def get_checklist(cid: str, version: int | None = None):
        return {**svc().checklist(cid, version).model_dump(mode="json"),
                "versions": svc().store.checklist_versions(cid)}

    @app.post("/api/checklists/validate")
    def validate_checklist(body: dict):
        from cqa.checklist import Checklist
        try:
            Checklist.model_validate(body)
        except ValidationError as e:
            return {"valid": False, "errors": [err["msg"] for err in e.errors()]}
        return {"valid": True, "errors": []}

    @app.post("/api/checklists")
    def save_checklist(body: dict):
        return svc().save_checklist(body).model_dump(mode="json")

    # ---------- dialogs ----------
    @app.post("/api/datasets/fetch")
    async def fetch_dataset(body: FetchBody):
        n = await svc().fetch_group(body.group_id.strip())
        return {"group_id": body.group_id, "imported": n}

    @app.post("/api/dialogs/upload")
    def upload(body: UploadBody):
        ids = svc().upload(body.content, body.group_id, body.channel, body.language, body.conversation_id)
        return {"conversation_ids": ids}

    @app.post("/api/dialogs/upload-audio")
    async def upload_audio(request: Request, mode: str = "local", language: str = "RU",
                           conversation_id: str | None = None,
                           approved_external_api: bool = False):
        if mode not in ("local", "cloud") or language not in ("RU", "KZ", "EN", "AUTO"):
            raise HTTPException(400, "Неверный режим STT или язык")
        if not stt_modes()[mode]["available"]:
            raise HTTPException(503, stt_modes()[mode].get("reason", "Локальный STT не настроен"))
        if mode == "cloud" and not approved_external_api:
            raise HTTPException(403, "Нужно разрешение на передачу аудио в Alem STT")
        cid = conversation_id or f"audio-{uuid.uuid4().hex[:10]}"
        if not SAFE_ID.fullmatch(cid):
            raise HTTPException(400, "Недопустимый ID диалога")
        if svc().store.get_dialog(cid):
            raise HTTPException(409, "Диалог с таким ID уже существует")
        size = request.headers.get("content-length")
        if size and size.isdecimal() and int(size) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Файл больше 50 МБ")
        body = await request.body()
        if not body or len(body) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "Нужен MP3-файл размером до 50 МБ")
        if not (body.startswith(b"ID3") or (len(body) > 1 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0)):
            raise HTTPException(415, "Поддерживается MP3-файл")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        temp = UPLOADS / f"{job_id}.tmp.mp3"
        target = UPLOADS / f"{cid}.mp3"
        if target.exists():
            raise HTTPException(409, "Файл с таким ID уже существует")
        temp.write_bytes(body)
        job = {"job_id": job_id, "status": "running", "stage": "queued", "conversation_id": cid}
        audio_jobs[job_id] = job
        active_service = svc()
        loop = asyncio.get_running_loop()

        async def work():
            try:
                async with audio_lock:
                    def progress(stage: str):
                        loop.call_soon_threadsafe(job.update, {"stage": stage})
                    record = await asyncio.to_thread(process_audio, temp, mode,
                                                     None if language == "AUTO" else language, progress)
                    job["stage"] = "saving"
                    record.update({"conversation_id": cid, "language": language,
                                   "audio_url": f"local:{cid}"})
                    os.replace(temp, target)
                    active_service.upload(record, group_id="UPLOAD", channel="voice call",
                                          language=language, conversation_id=cid)
                    job.update(status="done", stage="done")
            except Exception as exc:
                target.unlink(missing_ok=True)
                job.update(status="error", stage="error", error=str(exc))
            finally:
                temp.unlink(missing_ok=True)

        asyncio.create_task(work())
        return job

    @app.get("/api/audio-jobs/{job_id}")
    def audio_job(job_id: str):
        if job_id not in audio_jobs:
            raise HTTPException(404, "Задача не найдена")
        return audio_jobs[job_id]

    @app.get("/api/groups")
    def groups():
        return svc().store.groups()

    @app.get("/api/dialogs")
    def list_dialogs(group_id: str | None = None, checklist_id: str | None = None):
        s = svc()
        latest = s.store.latest_evaluations(checklist_id)
        out = []
        for d in s.store.list_dialogs(group_id):
            ev = latest.get(d["conversation_id"])
            summary = None
            if ev:
                view = s.evaluation_view(ev["id"])
                summary = {
                    "evaluation_id": ev["id"], "checklist_id": ev["checklist_id"],
                    "checklist_version": ev["checklist_version"], "created_at": ev["created_at"],
                    "total": (ev["score"] or {}).get("total"),
                    "final_total": (view["final_score"] or {}).get("total"),
                    "gross_violation": (view["final_score"] or ev["score"] or {}).get("gross_violation"),
                    "needs_review": [c["criterion_id"] for c in ev["criteria"] if c["status"] != "ok"],
                    "low_confidence": [c["criterion_id"] for c in ev["criteria"]
                                       if c["status"] == "ok" and (c.get("confidence") or 0) < 0.67],
                    "open_appeals": sum(1 for c in view["criteria"] for a in c["appeals"] if a["status"] == "open"),
                    "changed": view["changed"],
                }
            out.append({**d, "evaluation": summary})
        return out

    @app.get("/api/dialogs/{conversation_id}")
    def get_dialog(conversation_id: str, checklist_id: str | None = None):
        s = svc()
        view = s.dialog_view(conversation_id)
        ev = s.store.latest_evaluation(conversation_id, checklist_id)
        view["evaluation"] = s.evaluation_view(ev["id"]) if ev else None
        view["history"] = s.store.evaluation_history(conversation_id)
        return view

    @app.get("/api/dialogs/{conversation_id}/call-standard")
    def get_call_standard(conversation_id: str):
        row = svc().store.get_dialog(conversation_id)
        if not row:
            raise HTTPException(404, "Диалог не найден")
        return {"result": svc().store.get_call_standard(conversation_id)}

    @app.post("/api/dialogs/{conversation_id}/call-standard")
    async def run_call_standard(conversation_id: str, body: CallStandardBody):
        s = svc()
        row = s.store.get_dialog(conversation_id)
        if not row:
            raise HTTPException(404, "Диалог не найден")
        dialog = normalize(row["raw"])
        if not dialog.is_call:
            raise HTTPException(400, "Стандарт применим только к звонкам")
        if body.provider not in ("primary", "local"):
            raise HTTPException(400, "Неизвестный провайдер")
        if body.provider != "local" and not body.approved_external_api:
            raise HTTPException(403, "Нужно разрешение на передачу транскрипта внешнему API")
        chain = s._chain(body.provider)
        async with s.client_factory(chain) as client:
            try:
                result = await analyze_call(dialog, client)
            except ValueError as exc:
                raise ServiceError(str(exc)) from exc
        s.store.save_call_standard(conversation_id, result)
        return {"result": s.store.get_call_standard(conversation_id)}

    @app.get("/api/dialogs/{conversation_id}/audio")
    async def audio(conversation_id: str, request: Request):
        row = svc().store.get_dialog(conversation_id)
        if row and row["raw"].get("audio_url") == f"local:{conversation_id}":
            url = str(request.url_for("uploaded_audio_file", conversation_id=conversation_id))
            return {"src": url, "page": url}
        return await svc().audio_source(conversation_id)

    @app.get("/api/dialogs/{conversation_id}/audio-file", name="uploaded_audio_file")
    def uploaded_audio_file(conversation_id: str):
        if not SAFE_ID.fullmatch(conversation_id):
            raise HTTPException(404, "Запись не найдена")
        row = svc().store.get_dialog(conversation_id)
        file = UPLOADS / f"{conversation_id}.mp3"
        if not row or row["raw"].get("audio_url") != f"local:{conversation_id}" or not file.is_file():
            raise HTTPException(404, "Запись не найдена")
        return FileResponse(file, media_type="audio/mpeg")

    # ---------- evaluation jobs ----------
    @app.post("/api/evaluations")
    async def start_evaluation(body: EvalBody):
        job = svc().start_job(body.conversation_ids, body.checklist_id, body.checklist_version,
                              body.samples, body.provider, body.force)
        return {"job_id": job}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return svc().job_view(job_id)

    @app.get("/api/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: int):
        return svc().evaluation_view(evaluation_id)

    # ---------- appeals ----------
    @app.post("/api/evaluations/{evaluation_id}/appeals")
    def add_appeal(evaluation_id: int, body: AppealBody):
        aid = svc().add_appeal(evaluation_id, body.criterion_id, body.comment, body.author)
        return {"appeal_id": aid, "evaluation": svc().evaluation_view(evaluation_id)}

    @app.get("/api/appeals")
    def appeals(status: str | None = "open"):
        return svc().appeals_queue(None if status == "all" else status)

    @app.post("/api/appeals/{appeal_id}/decision")
    def decide(appeal_id: int, body: DecisionBody):
        return svc().decide(appeal_id, body.decision, body.reason, body.answer, body.value, body.reviewer)

    @app.get("/api/export/results")
    def export_results(group: str = "DEV", checklist_id: str = "freedom_default", version: int | None = None,
                       team_name: str | None = None):
        return svc().export_results(group, checklist_id, version, team_name)

    @app.get("/api/quality")
    def quality():
        return svc().quality()

    # ---------- frontend ----------
    if WEB.exists():
        app.mount("/static", StaticFiles(directory=WEB), name="static")

        @app.get("/")
        def index():
            return FileResponse(WEB / "index.html")

    return app


app = create_app()
