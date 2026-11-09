from copy import deepcopy

import pytest

from logimate.routing_eval import score, validate_dataset
from scripts.prepare_routing_eval import build


def test_dataset_is_separate_from_business_fixtures():
    parsed = validate_dataset(build())
    assert len(parsed.examples) == 24
    assert all("SIM-00" not in c.query for c in parsed.examples)


def test_missing_predictions_count_as_failures():
    result = score(build(), {}, "holdout")
    assert result["counts"]["total"] == result["counts"]["missing"] == 12
    assert result["metrics"]["all_exact_rate"] == 0


def test_metric_denominator_includes_missing_and_invalid():
    data = build()
    cases = [c for c in data["examples"] if c["split"] == "dev"]
    predictions = {cases[0]["case_id"]: cases[0]["gold"], cases[1]["case_id"]: {"action": "execute_refund"}}
    result = score(data, predictions, "dev")
    assert result["counts"]["all_exact"] == 1
    assert result["counts"]["invalid"] == 1
    assert result["counts"]["missing"] == 10
    assert result["metrics"]["all_exact_rate"] == 1/12


def test_family_leakage_rejected():
    data = deepcopy(build())
    data["examples"][0]["split"] = "holdout"
    with pytest.raises(ValueError, match="family leakage"):
        validate_dataset(data)


def test_foreign_split_predictions_rejected():
    data = build()
    case = next(c for c in data["examples"] if c["split"] == "holdout")
    with pytest.raises(ValueError, match="outside selected split"):
        score(data, {case["case_id"]: case["gold"]}, "dev")


def test_wrong_domain_is_recorded():
    data = build()
    case = data["examples"][0]
    result = score(data, {case["case_id"]: {**case["gold"], "domains": ["logistics"]}}, "dev")
    assert result["counts"]["action_correct"] == 1
    assert result["counts"]["slots_exact"] == 1
    assert result["counts"]["all_exact"] == 0
    assert result["failures"][0]["fields"] == ["domains_exact"]
