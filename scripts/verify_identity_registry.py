#!/usr/bin/env python3
"""Verify an external agent identity registry against an independently held head.

Usage: python scripts/verify_identity_registry.py PATH LEDGER_GENESIS EXPECTED_HEAD
For an empty registry, pass the literal word EMPTY as EXPECTED_HEAD.
The head must come from a separate trust channel, not the registry under test.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neural_mesh.identity_registry import verify_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('ledger_genesis')
    parser.add_argument('expected_head', help='independently retained SHA-256 head, or EMPTY')
    args = parser.parse_args()
    head = '' if args.expected_head == 'EMPTY' else args.expected_head
    if head and (len(head) != 64 or any(c not in '0123456789abcdef' for c in head)):
        parser.error('expected_head must be 64 lowercase hex characters or EMPTY')
    result = verify_registry(args.path, args.ledger_genesis, expected_head=head)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
