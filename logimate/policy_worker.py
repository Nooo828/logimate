"""One bounded retrieval batch per isolated process; stdout is a single JSON payload."""
import json
import sys

from .policy_tools import PolicyArguments, retrieve
from .tool_gateway import Principal


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(8192))
        principal = Principal(request['tenant_id'], frozenset(request['domains']))
        args = PolicyArguments.model_validate(request['arguments'])
        result = retrieve(principal, args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception:
        print(json.dumps({'error': 'RETRIEVAL_UNAVAILABLE'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
