"""Frozen deterministic aggregation baseline. Not an LLM or original teacher code."""


def aggregate(snapshot):
    return {
        "implementation": "aggregation-v0",
        "simulated": True,
        "order_id": snapshot.order.order_id,
        "records": snapshot.model_dump(mode="json"),
        "diagnosis": None,
        "note": "Only aggregates records; an operator must compare them manually.",
    }
