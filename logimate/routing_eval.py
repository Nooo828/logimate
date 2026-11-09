"""Score external structured routing predictions. Never invent missing predictions."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["read", "clarify", "human", "refuse"]
    order_ids: list[str]
    domains: list[Literal["order", "logistics", "warehouse", "aftersales"]]


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    family: str
    split: Literal["dev", "holdout"]
    query: str = Field(min_length=1)
    gold: Route


class RoutingDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    simulated: Literal[True]
    examples: list[Case]


def validate_dataset(data):
    parsed = RoutingDataset.model_validate(data)
    ids = [c.case_id for c in parsed.examples]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case_id")
    families = {}
    queries = set()
    for c in parsed.examples:
        if c.family in families and families[c.family] != c.split:
            raise ValueError("family leakage between dev and holdout")
        families[c.family] = c.split
        if c.query in queries:
            raise ValueError("duplicate query")
        queries.add(c.query)
    if {c.split for c in parsed.examples} != {"dev", "holdout"}:
        raise ValueError("both splits required")
    return parsed


def score(dataset, predictions, split):
    parsed = validate_dataset(dataset)
    cases = [c for c in parsed.examples if c.split == split]
    if not cases:
        raise ValueError("empty or unknown split")
    unknown = set(predictions) - {c.case_id for c in cases}
    if unknown:
        raise ValueError("prediction ids outside selected split")
    counts = dict(total=len(cases), missing=0, invalid=0, action_correct=0, slots_exact=0, domains_exact=0, all_exact=0)
    failures = []
    for c in cases:
        if c.case_id not in predictions:
            counts["missing"] += 1
            failures.append({"case_id": c.case_id, "reason": "missing_prediction"})
            continue
        try:
            p = Route.model_validate(predictions[c.case_id])
            if len(p.order_ids) != len(set(p.order_ids)) or len(p.domains) != len(set(p.domains)):
                raise ValueError("duplicate slots/domains")
            if p.action != "read" and p.domains:
                raise ValueError("non-read action cannot request tools")
        except ValueError:
            counts["invalid"] += 1
            failures.append({"case_id": c.case_id, "reason": "invalid_prediction"})
            continue
        correct = {"action_correct": p.action == c.gold.action,
                   "slots_exact": set(p.order_ids) == set(c.gold.order_ids),
                   "domains_exact": set(p.domains) == set(c.gold.domains)}
        for key, value in correct.items():
            counts[key] += int(value)
        counts["all_exact"] += int(all(correct.values()))
        if not all(correct.values()):
            failures.append({"case_id": c.case_id, "reason": "mismatch", "fields": [k for k,v in correct.items() if not v]})
    return {"dataset_id": parsed.dataset_id, "split": split, "simulated": True, "counts": counts,
            "metrics": {key+"_rate": counts[key]/len(cases) for key in ("action_correct", "slots_exact", "domains_exact", "all_exact")},
            "failures": failures, "note": "Synthetic routing/slot evaluation only; no business benefit or answer-quality claim."}
