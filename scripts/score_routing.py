"""Score a supplied JSON prediction map. Never runs or simulates a model."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from logimate.routing_eval import score

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "holdout"], default="dev")
    args = parser.parse_args()
    dataset = ROOT / "eval/routing_v1.json"
    result = score(json.loads(dataset.read_text(encoding="utf-8")), json.loads(args.predictions.read_text(encoding="utf-8")), args.split)
    result["input_sha256"] = {"dataset": hashlib.sha256(dataset.read_bytes()).hexdigest(), "predictions": hashlib.sha256(args.predictions.read_bytes()).hexdigest()}
    out = ROOT / "docs/evidence/p2" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    out.mkdir(parents=True, exist_ok=False)
    (out/"routing-scores.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(out/"routing-scores.json")


if __name__ == "__main__":
    main()
