"""Offline verifier for complete, non-empty mesh-ledger-v1 event exports."""
import argparse
import json
import math
from pathlib import Path
import sys

# Import only the shipped stdlib ledger module, never initialize a Mesh.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'neural_mesh'))
from importlib import import_module

ledger = import_module('ledger')
ProvenanceLedger, SCHEME, merkle_root = ledger.ProvenanceLedger, ledger.SCHEME, ledger.merkle_root


def unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError('duplicate JSON key')
        result[k] = v
    return result


def verify(b, pin):
    if not isinstance(b, dict) or b.get('scheme') != SCHEME or b.get('kind') != 'ledger-bundle':
        raise ValueError('expected a mesh-ledger-v1 ledger-bundle')
    rows, head = b.get('entries'), b.get('head')
    if not isinstance(rows, list) or not rows or not isinstance(head, dict):
        raise ValueError('need a non-empty full export with a head')
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict) or type(row.get('seq')) is not int or row['seq'] != i:
            raise ValueError('need contiguous entries starting at sequence 1')
        if not all(isinstance(row.get(k), str) for k in ('op','node_id','agent_id','source','digest','prev_hash','entry_hash')) or not isinstance(row.get('detail'), dict):
            raise ValueError(f'invalid event fields at sequence {i}')
        if type(row.get('ts')) not in (float, int) or not math.isfinite(row['ts']):
            raise ValueError(f'invalid timestamp at sequence {i}')
    if any(type(head.get(k)) is not int or head[k] != len(rows) for k in ('entries','seq')):
        raise ValueError('head count/sequence does not match complete export')
    if not (b.get('chain_id') == head.get('chain_id') == rows[0]['entry_hash']):
        raise ValueError('chain identity does not match genesis')
    if head.get('entry_hash') != rows[-1]['entry_hash'] or head.get('ts') != rows[-1]['ts']:
        raise ValueError('claimed head does not match last event')
    result = ProvenanceLedger.verify_bundle(b)
    if not result['ok']:
        return result
    if merkle_root(r['entry_hash'] for r in rows) != b.get('merkle_root'):
        raise ValueError('Merkle root mismatch')
    if pin is not None and pin != head['entry_hash']:
        raise ValueError('independently supplied head does not match')
    return {**result, 'reason': 'event hashes, links and root agree', 'head_hash': head['entry_hash'], 'head_pin_checked': pin is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--expect-head', help='exact head hash obtained separately')
    args = parser.parse_args()
    try:
        with args.bundle.open(encoding='utf-8') as f:
            result = verify(json.load(f, object_pairs_hook=unique, parse_constant=reject_constant), args.expect_head)
    except (OSError, ValueError, OverflowError, RecursionError) as e:
        result = {'ok': False, 'reason': str(e)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


def reject_constant(value):
    raise ValueError(f'non-finite JSON constant: {value}')


if __name__ == '__main__':
    raise SystemExit(main())
