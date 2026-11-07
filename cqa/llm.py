"""OpenAI-compatible chat client (Alem Plus, DeepSeek, Ollama, ...) with retries and fallback.

Providers come from environment variables (see .env.example):
  LLM_*       primary provider
  FALLBACK_*  used when the primary fails after retries
  LOCAL_*     optional local provider (e.g. Ollama: http://localhost:11434/v1)
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    supports_json_mode: bool = True
    price_in_per_m: float = 0.0   # USD per 1M input tokens (0 = free)
    price_out_per_m: float = 0.0

    @classmethod
    def from_env(cls, prefix: str, name: str) -> "Provider | None":
        base = os.environ.get(f"{prefix}_BASE_URL")
        model = os.environ.get(f"{prefix}_MODEL")
        if not base or not model:
            return None
        return cls(
            name=name,
            base_url=base.rstrip("/"),
            api_key=os.environ.get(f"{prefix}_API_KEY", "none"),
            model=model,
            price_in_per_m=float(os.environ.get(f"{prefix}_PRICE_IN", 0) or 0),
            price_out_per_m=float(os.environ.get(f"{prefix}_PRICE_OUT", 0) or 0),
        )

    def public(self) -> dict:
        return {"name": self.name, "model": self.model, "base_url": self.base_url}


def providers_from_env() -> dict[str, Provider]:
    load_env()
    out = {}
    for prefix, name in (("LLM", "primary"), ("FALLBACK", "fallback"), ("LOCAL", "local")):
        p = Provider.from_env(prefix, name)
        if p:
            out[name] = p
    return out


@dataclass
class LLMCall:
    provider: str
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    ok: bool
    error: str | None = None


@dataclass
class LLMResponse:
    content: str
    calls: list[LLMCall] = field(default_factory=list)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, providers: list[Provider], concurrency: int = 6, timeout: float = 120.0,
                 retries: int = 2):
        if not providers:
            raise LLMError("no LLM provider configured (set LLM_BASE_URL / LLM_MODEL in .env)")
        self.providers = providers
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.retries = retries
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LLMClient":
        self._http = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http:
            await self._http.aclose()

    async def _once(self, p: Provider, messages: list[dict], temperature: float,
                    max_tokens: int) -> tuple[str, LLMCall]:
        body = {"model": p.model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens}
        if p.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        r = await self._http.post(f"{p.base_url}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {p.api_key}"})
        if r.status_code == 400 and p.supports_json_mode and "response_format" in r.text:
            p.supports_json_mode = False  # provider doesn't support JSON mode; prompt still demands JSON
            return await self._once(p, messages, temperature, max_tokens)
        latency = time.monotonic() - t0
        if r.status_code >= 400:
            raise LLMError(f"{p.name} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        cost = pt / 1e6 * p.price_in_per_m + ct / 1e6 * p.price_out_per_m
        return content, LLMCall(p.name, p.model, round(latency, 2), pt, ct, round(cost, 6), True)

    async def chat(self, messages: list[dict], temperature: float = 0.0,
                   max_tokens: int = 2000) -> LLMResponse:
        calls: list[LLMCall] = []
        async with self.sem:
            for p in self.providers:
                for attempt in range(self.retries + 1):
                    try:
                        content, call = await self._once(p, messages, temperature, max_tokens)
                        calls.append(call)
                        if content.strip():
                            return LLMResponse(content, calls)
                        raise LLMError(f"{p.name}: empty response")
                    except (httpx.HTTPError, LLMError, KeyError, ValueError) as e:
                        calls.append(LLMCall(p.name, p.model, 0, 0, 0, 0, False, str(e)[:300]))
                        if attempt < self.retries:
                            await asyncio.sleep(1.5 * (attempt + 1))
        raise LLMError("all providers failed: " + "; ".join(c.error or "" for c in calls if not c.ok))
