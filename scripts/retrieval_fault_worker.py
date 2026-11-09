"""Explicit test-only stalled RPC process. Never selected by a production API request."""
import argparse
import json
import sys
import time

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['primary', 'keyword'], required=True)
    role = parser.parse_args().role
    print(json.dumps({'type': 'ready', 'role': role}), flush=True)
    for line in sys.stdin.buffer:
        time.sleep(30)
