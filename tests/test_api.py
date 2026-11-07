import copy
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from cqa.llm import Provider
from cqa.service import Service
from cqa.store import Store
from tests.test_evaluator import ALL_OK, FakeClient, j
from tests.test_transcript_facts import CHAT


class FakeCtx(FakeClient):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture()
def env():
    script = copy.deepcopy(ALL_OK)
    clients: list[FakeCtx] = []

    def factory(chain):
        c = FakeCtx(script)
        clients.append(c)
        return c

    prov = {"primary": Provider("primary", "http://x", "k", "fake-model")}
    svc = Service(Store(":memory:"), providers=prov, default_samples=1, client_factory=factory)
    with TestClient(create_app(svc)) as client:
        svc.upload({**CHAT, "conversation_id": "chat-1"}, group_id="T")
        yield client, svc, script, clients


def wait_job(client, job_id):
    for _ in range(100):
        j_ = client.get(f"/api/jobs/{job_id}").json()
        if j_["status"] != "running":
            return j_
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def evaluate(client, ids, checklist="freedom_default"):
    r = client.post("/api/evaluations", json={"conversation_ids": ids, "checklist_id": checklist})
    assert r.status_code == 200, r.text
    return wait_job(client, r.json()["job_id"])


def test_full_flow_evaluate_appeal_decide(env):
    client, svc, script, clients = env
    job = evaluate(client, ["chat-1"])
    assert job["status"] == "done" and not job["failed"]
    assert job["results"]["chat-1"]["total"] == 100.0

    d = client.get("/api/dialogs/chat-1").json()
    ev = d["evaluation"]
    assert ev["final_score"]["total"] == 100.0
    c07 = next(c for c in ev["criteria"] if c["criterion_id"] == "c07")
    assert c07["evidence"][0]["from_id"] == "m4"

    # operator disputes c05, reviewer changes it to "no"
    r = client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c05", "comment": "не так"})
    assert r.status_code == 200
    queue = client.get("/api/appeals").json()
    assert len(queue) == 1 and queue[0]["ai_answer"] == "yes"
    r = client.post(f"/api/appeals/{queue[0]['id']}/decision",
                    json={"decision": "change", "answer": "no", "reason": "не отреагировал на проблему"})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["score"]["total"] == 100.0            # original AI score is kept
    assert view["final_score"]["total"] == 85.0       # final after the decision
    c05 = next(c for c in view["criteria"] if c["criterion_id"] == "c05")
    assert c05["answer"] == "yes" and c05["final_answer"] == "no"
    assert client.get("/api/appeals").json() == []
    q = client.get("/api/quality").json()
    assert q["reviewer_agreement"]["c05"] == {"reviewed": 1, "changed": 1, "agreement": 0.0}


def test_invalid_decision_rejected(env):
    client, *_ = env
    evaluate(client, ["chat-1"])
    ev = client.get("/api/dialogs/chat-1").json()["evaluation"]
    client.post(f"/api/evaluations/{ev['id']}/appeals", json={"criterion_id": "c04", "comment": "x"})
    aid = client.get("/api/appeals").json()[0]["id"]
    r = client.post(f"/api/appeals/{aid}/decision", json={"decision": "change", "answer": "score",
                                                          "value": 12, "reason": "r"})
    assert r.status_code == 400 and "ступен" in r.json()["detail"]


def test_replacing_a_criterion_reevaluates_only_it(env):
    client, svc, script, clients = env
    evaluate(client, ["chat-1"])
    cl = client.get("/api/checklists/freedom_default").json()
    cl.pop("versions")
    cl["criteria"] = [c for c in cl["criteria"] if c["id"] != "c05"] + [{
        "id": "c09", "section": "conduct", "title": "Персонализация", "type": "binary", "weight": 15,
        "description": "Да — оператор обратился к клиенту по имени.",
    }]
    script["Персонализация"] = [j("no", ev=("m4", "m4"))]
    saved = client.post("/api/checklists", json=cl).json()
    assert saved["version"] == 2

    calls_before = len(clients[-1].prompts)
    job = evaluate(client, ["chat-1"])
    assert job["results"]["chat-1"]["total"] == 85.0
    assert len(clients[-1].prompts) == 1 and calls_before == 8   # only c09 went to the LLM
    ev = client.get("/api/dialogs/chat-1").json()["evaluation"]
    assert ev["checklist_version"] == 2 and ev["evaluated_criteria"] == ["c09"]
    assert len(ev["cached_criteria"]) == 7


def test_invalid_checklist_is_rejected(env):
    client, *_ = env
    bad = {"id": "x", "name": "x", "criteria": [{"id": "a", "title": "A", "type": "scale", "weight": 10,
                                                  "description": "d", "scale": [{"value": 1, "description": "x"}]}]}
    assert client.post("/api/checklists/validate", json=bad).json()["valid"] is False
    assert client.post("/api/checklists", json=bad).status_code == 422


def test_plain_text_upload_and_list(env):
    client, *_ = env
    r = client.post("/api/dialogs/upload", json={"content": "Клиент: привет\nОператор: Здравствуйте, я Дана",
                                                 "conversation_id": "txt-1"})
    assert r.json()["conversation_ids"] == ["txt-1"]
    ids = [d["conversation_id"] for d in client.get("/api/dialogs").json()]
    assert ids == ["chat-1", "txt-1"]
    assert client.get("/api/dialogs/txt-1").json()["dialog"]["messages"][1]["speaker"] == "operator"


def test_export_results_format(env):
    client, svc, script, clients = env
    svc.upload({**CHAT, "conversation_id": "chat-2"}, group_id="T")
    evaluate(client, ["chat-1"])  # chat-2 stays unevaluated -> status "error"
    out = client.get("/api/export/results", params={"group": "T", "team_name": "Team"}).json()
    assert out["problems"] == []
    data = out["results"]
    assert data["schema_version"] == "1.0" and data["team_name"] == "Team"
    assert data["model"]["mode"] == "external_api" and data["model"]["name"]
    by_id = {r["conversation_id"]: r for r in data["results"]}
    assert set(by_id) == {"chat-1", "chat-2"}
    ok = by_id["chat-1"]
    assert ok["status"] == "ok" and ok["score"] == {"final_score": 100.0, "critical_flag": False}
    codes = {c["criterion_id"]: c for c in ok["criteria"]}
    assert sorted(codes) == sorted(["contact", "discovery", "identification", "consultation",
                                    "empathy", "dialogue", "closure", "critical"])
    assert codes["critical"]["value"] is False
    assert all(isinstance(c["evidence_turns"], list) and c["evidence_turns"] for c in ok["criteria"])
    err = by_id["chat-2"]
    assert err == {"conversation_id": "chat-2", "status": "error", "criteria": [], "score": None,
                   "error": "Диалог не оценён."}


def test_validate_results_catches_bad_values():
    from cqa.export import validate_results
    bad = {"schema_version": "1.0", "team_name": "t", "model": {"mode": "cloud", "name": "x"},
           "results": [{"conversation_id": "a", "status": "ok", "score": {"final_score": 50, "critical_flag": True},
                        "criteria": [{"criterion_id": "dialogue", "value": 5, "evidence_turns": [0]}]},
                       {"conversation_id": "a", "status": "error", "criteria": [], "score": None, "error": "x"}]}
    errs = " | ".join(validate_results(bad, ["a", "b"]))
    for frag in ("model.mode", "повторяются ID: a", "нет диалогов: b", "все 8 критериев", "dialogue: недопустимое",
                 "evidence_turns", "critical_flag"):
        assert frag in errs, frag
