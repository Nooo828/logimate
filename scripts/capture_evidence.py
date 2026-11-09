"""Capture reproducible fixture demonstration; this is not a real-world evaluation."""
import hashlib
import json
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from baselines.v0 import aggregate
from logimate.repository import Repository
from logimate.rules import diagnose

ROOT = Path(__file__).resolve().parents[1]
repo = Repository()
out = ROOT / "docs" / "evidence"
out.mkdir(parents=True, exist_ok=True)
baseline = aggregate(repo.get("SIM-002", "demo-a"))
(out / "baseline-v0.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
results = [diagnose(s, as_of=repo.dataset.as_of, dataset_id=repo.dataset.dataset_id, intent="overall").model_dump(mode="json")
           for s in repo.dataset.snapshots if s.order.tenant_id == "demo-a"]
(out / "rules-v1.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
manifest = {"captured_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "platform": platform.platform(), "packages": {p: version(p) for p in ["fastapi", "uvicorn", "pytest", "httpx", "pydantic"]},
            "dataset_sha256": repo.sha256, "simulated": True, "baseline_kind": "deterministic aggregation, not single LLM agent",
            "files": {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                      for folder in ["logimate", "baselines", "data", "tests", "scripts"] for p in sorted((ROOT/folder).glob('**/*'))
                      if p.is_file() and '__pycache__' not in str(p)},
            "not_measured": ["LLM quality/token/cost", "business time savings", "retrieval recall", "real traffic", "ticket reduction"]}
(out / "reproduction-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
print("Captured baseline and v1 fixture results, with source hashes.")
