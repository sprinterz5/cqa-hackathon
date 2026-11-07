import json

from fastapi.testclient import TestClient

from app.main import create_app
from cqa.llm import LLMResponse, Provider
from cqa.service import Service
from cqa.store import Store


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def chat(self, messages, **kwargs):
        assert "ТРАНСКРИПТ" in messages[0]["content"]
        payload = {"questions": [{"answer": "Да", "explanation": "Слышно в звонке",
                                  "evidence": ["m2"]} for _ in range(8)],
                   "issues": [{"title": "Тариф", "outcome": "Решён — клиент подтвердил",
                               "evidence": ["m1", "m2"]}],
                   "gross_violation": {"found": False, "explanation": "", "evidence": []}}
        return LLMResponse(json.dumps(payload, ensure_ascii=False))


def test_call_standard_is_separate_and_requires_external_api_permission():
    store = Store(":memory:")
    record = {"conversation_id": "call-1", "channel": "voice call", "language": "RU",
              "audio_url": None, "transcript": [
                  {"speaker": "client", "timestamp": "00:00", "text": "Какой тариф?"},
                  {"speaker": "operator", "timestamp": "00:01", "text": "Тариф один процент."}]}
    store.upsert_dialog(record, "DEV", "test")
    provider = Provider("primary", "https://example.invalid", "key", "fake")
    service = Service(store, providers={"primary": provider}, client_factory=lambda _: FakeClient())
    with TestClient(create_app(service)) as client:
        path = "/api/dialogs/call-1/call-standard"
        assert client.get(path).json()["result"] is None
        assert client.post(path, json={"provider": "primary"}).status_code == 403
        response = client.post(path, json={"provider": "primary", "approved_external_api": True})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["score"] == 100.0 and result["preliminary"] is True
        assert result["status"] == "Не определено"
        assert result["questions"][3]["answer"] == "Недостаточно данных"
        assert result["issues"][0]["evidence"] == ["m1", "m2"]
        assert client.get(path).json()["result"] == result
        assert client.get("/api/dialogs/call-1").json()["evaluation"] is None
