"""Repeat only the role step on the five DEV calls, with and without phrase splitting, to separate
the effect of splitting from run-to-run variance of the LLM. Reuses the Alem phrases of the stored
<id>-asr dialogs' audio (one STT call per recording), no evaluation."""
import asyncio, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from asr_check import reference_line_roles
from cqa.llm import LLMClient
from cqa.service import Service
from cqa.store import Store
from cqa.stt import assign_roles, merge_segments, transcribe
from cqa.transcript import normalize

async def main():
    svc = Service(Store(ROOT / "data" / "cqa.db"))
    chain = svc._chain("primary")
    ids = [d["conversation_id"] for d in svc.store.list_dialogs("DEV") if d["channel"] == "voice call"]
    phrases = {}
    for cid in ids:
        f = svc.audio_dir / f"{cid}-asr.mp3"
        phrases[cid] = (await transcribe(svc.stt, f.read_bytes(), f.name, None))["segments"]
    out = {}
    async with LLMClient(chain) as client:
        for split in (True, False):
            for run in (1, 2):
                tot = [0, 0]
                for cid in ids:
                    segs, roles, src, _ = await assign_roles(client, phrases[cid], split=split)
                    pred = normalize({"conversation_id": cid, "channel": "voice call",
                                      "transcript": merge_segments(segs, roles, src)})
                    m = reference_line_roles(normalize(svc.store.get_dialog(cid)["raw"]), pred)
                    tot[0] += m["correct"]; tot[1] += m["aligned"]
                    out.setdefault(f"split={split}", {}).setdefault(cid, []).append(m["correct"])
                print(f"split={split} run={run}: {tot[0]}/{tot[1]} = {tot[0] / tot[1]:.1%}", flush=True)
    print(json.dumps(out, ensure_ascii=False))

asyncio.run(main())
