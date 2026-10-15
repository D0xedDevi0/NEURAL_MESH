"""Optional Jev reranking annotation over already-retrieved NEURAL_MESH candidates.

Shadow-only: the production recall order and scores are never changed. The
projection exports a bounded, operator-attested subset of node-card fields and
never surfaces embeddings, raw metadata, or full memory content. Hosted
inference requires explicit consent and is otherwise offline with no call.

Pure stdlib for the projection; the reviewed Architect decision engine is
imported lazily only when a hosted call is actually requested.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

MAX_EXCERPT = 512
MAX_QUERY = 1024
MAX_CANDIDATES = 6
_PROOF_BOUND = 128
_CHOICE_PREFIX = 'inspect:'
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_LANE = re.compile(r'[A-Za-z0-9_-]{1,32}')
# Secret-shaped VALUES (not identifier substrings): URLs, emails, 32+ alnum runs,
# vendor prefixes, credential words. Same lesson as the REA triage adapter.
_SECRET = re.compile(
    r'https?://|@|[A-Za-z0-9_-]{32,}|password|secret|bearer|authorization|'
    r'private[ _-]?key|gh[pousr]_|sk[-_][A-Za-z0-9]|AKIA|BEGIN [A-Z ]*PRIVATE KEY',
    re.I,
)
# Public proof fields that are safe to project and relevant to a first-inspection
# relevance judgment. URL-shaped fields (url/source/full tx) are intentionally
# excluded: they collide with the secret filter and are not needed for relevance.
_PROOF_KEYS = (
    'proof_type', 'claim', 'trust', 'by', 'provenance', 'network', 'chain_id',
    'tx_short', 'term_id_short', 'block', 'agent_id', 'aura_score', 'verified',
)


def _printable(text: str, limit: int) -> bool:
    return isinstance(text, str) and 0 < len(text.encode()) <= limit and all(32 <= ord(c) < 127 for c in text)


def _bounded(value, limit: int) -> str | None:
    if not isinstance(value, str) or not value or not _printable(value, limit) or _SECRET.search(value):
        return None
    return value[:limit]


def _safe_excerpt(content) -> tuple[str | None, bool]:
    """Return (excerpt, withheld). Withheld when content is empty, non-printable,
    oversized, or secret-shaped; the candidate stays listed without its text."""
    if not isinstance(content, str) or not content.strip():
        return None, False
    if _SECRET.search(content[:MAX_EXCERPT * 2]) or not _printable(content, MAX_EXCERPT * 4):
        return None, True
    return content[:MAX_EXCERPT], False


def _proof_fields(proof) -> dict | None:
    if not isinstance(proof, dict):
        return None
    out = {}
    for key in _PROOF_KEYS:
        value = proof.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if not _bounded(value, _PROOF_BOUND):
                continue
            out[key] = value
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int):
            out[key] = value
        elif isinstance(value, float):
            if value != value or value in (float('inf'), float('-inf')):
                continue
            out[key] = value
    return out or None


def project_recall(results, query: str) -> dict:
    """Reduce a recall result (list of node cards or the dict from
    recall_with_proofs) to a bounded, operator-attested projection."""
    if not _printable(query, MAX_QUERY) or not query.strip():
        raise ValueError('query')
    if isinstance(results, dict):
        results = results.get('results')
    if not isinstance(results, list):
        raise ValueError('recall_results')
    seen = set()
    candidates = []
    withheld = 0
    for card in results[:MAX_CANDIDATES]:
        if not isinstance(card, dict):
            raise ValueError('card_shape')
        node_id = card.get('id')
        if not isinstance(node_id, str) or not _ID.fullmatch(node_id):
            raise ValueError('node_id')
        if node_id in seen:
            raise ValueError('duplicate_node_id')
        seen.add(node_id)
        lane = card.get('lane')
        trust = card.get('trust')
        try:
            trust = float(trust) if trust is not None else None
            if trust is not None and (trust != trust or trust in (float('inf'), float('-inf'))):
                trust = None
        except (TypeError, ValueError):
            trust = None
        excerpt, content_withheld = _safe_excerpt(card.get('content'))
        if content_withheld:
            withheld += 1
        raw_type = card.get('type')
        entry = {
            'id': node_id,
            'type': _bounded(raw_type, 64) if isinstance(raw_type, str) else None,
            'trust': trust,
            'provenance': _bounded(card.get('provenance'), 256),
            'lane': lane if isinstance(lane, str) and _LANE.fullmatch(lane) else None,
        }
        if excerpt is not None:
            entry['excerpt'] = excerpt
        else:
            entry['content_withheld'] = content_withheld
        proof = _proof_fields(card.get('proof'))
        if proof:
            entry['proof'] = proof
        candidates.append(entry)
    return {
        'schema': 'mesh-rerank-projection-v1',
        'query': query,
        'source_kind': 'node-card-recall',
        'recall_order_preserved': True,
        'applied': False,
        'candidates': candidates,
        'candidate_count': len(candidates),
        'withheld_content': withheld,
        'omitted_candidates': len(results) - len(candidates),
        'source_fingerprint': hashlib.sha256(
            json.dumps(candidates, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
    }


def validate_origin(url: str) -> str:
    import ipaddress
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
        raise ValueError('public_source_url')
    try:
        p = urlsplit(url)
        host = p.hostname
        if (p.scheme not in ('http', 'https') or not host or p.username or p.password
                or p.query or p.fragment or p.port not in (None, 80 if p.scheme == 'http' else 443)):
            raise ValueError('public_source_url')
        if host.lower().rstrip('.') == 'localhost' or host.lower().rstrip('.').endswith('.localhost'):
            raise ValueError('public_source_url')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError('public_source_url')
    except (ValueError, TypeError):
        raise ValueError('public_source_url') from None
    return url


DEFAULT_SOURCE_URL = 'https://github.com/D0xedDevi0/NEURAL_MESH'


def rerank(results, query: str, *, allow_model_input: bool = False, offline: bool = False,
           source_url: str = DEFAULT_SOURCE_URL, engine=None) -> dict:
    if type(allow_model_input) is not bool or type(offline) is not bool:
        raise ValueError('consent_flags')
    source_url = validate_origin(source_url)
    projection = project_recall(results, query)
    result = {
        'schema': 'mesh-rerank-v1',
        'projection': projection,
        'shadow_only': True,
        'applied': False,
        'production_recall_unchanged': True,
        'memory_written': False,
        'policy_changes': False,
        'suggested_order': None,
    }

    def stop(outcome: str, reason: str):
        result['decision'] = {'outcome': outcome, 'reason_code': reason, 'attempts': 0}
        return result

    if offline:
        return stop('unavailable', 'offline_no_inference')
    if not allow_model_input:
        return stop('denied', 'explicit_public_input_consent_required')
    if not projection['candidates']:
        return stop('unavailable', 'no_eligible_candidates')
    try:
        from architect.decision_types import DecisionPolicy, Provenance
        from architect.decision_engine import DecisionEngine
        from architect.decision_provider import JevProvider
    except ImportError:
        return stop('unavailable', 'optional_architect_boundary_unavailable')

    policy = DecisionPolicy(model='jev-1.13.0', question_set='mesh-rerank-v1',
                            policy_version='mesh-shadow-v1', max_attempts=1,
                            max_candidates=MAX_CANDIDATES, max_questions=7)
    if engine is None:
        engine = DecisionEngine(JevProvider(), policy)
    rows = projection['candidates']
    questions = {}
    for row in rows:
        questions['relevance_' + row['id']] = {
            'type': 'score',
            'description': 'Relevance of candidate ' + row['id'] + ' to the query for first inspection; not proof of truth.',
            'criteria': ['Not informative for this query', 'Potentially informative; context missing',
                         'Directly useful to inspect first'],
        }
    criteria = {'none': 'No candidate is sufficiently supported as a useful first inspection'}
    criteria.update({_CHOICE_PREFIX + r['id']: 'Inspect candidate ' + r['id'] + ' first' for r in rows})
    questions['first'] = {
        'type': 'choice',
        'description': 'Choose one supported candidate to inspect first, or none. Do not authorize an action or assert truth.',
        'criteria': criteria,
    }
    provenance = Provenance(source_url, source_url, projection['source_fingerprint'], True,
                            tuple(rows), projection['omitted_candidates'])
    state = {
        'task': 'Review-only re-ranking of already-retrieved private-memory candidates; no truth or action authority.',
        'data_not_instruction': True,
        'query': query,
        'authority': 'operator-attested public projection of local recall',
        'system_provenance': 'source_url labels the system identity, not the memory location',
        'candidates': rows,
    }
    receipt = engine.evaluate(state, questions, provenance, policy).to_dict()
    result['decision'] = receipt
    if receipt.get('outcome') == 'ok':
        choice = receipt.get('answers', {}).get('first', {}).get('choice')
        if isinstance(choice, str) and choice.startswith(_CHOICE_PREFIX):
            picked = choice[len(_CHOICE_PREFIX):]
            if picked in {r['id'] for r in rows}:
                result['suggested_order'] = [picked]
    return result


def run_cli(args):
    from pathlib import Path
    raw = Path(args.recall).read_bytes()
    try:
        recall = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError('recall_json') from exc
    if isinstance(recall, dict) and 'results' in recall:
        results = recall['results']
    elif isinstance(recall, list):
        results = recall
    else:
        raise ValueError('recall_json')
    project_recall(results, args.query)  # preflight before output creation
    out = Path(os.path.abspath(args.out))
    if not out.parent.is_dir():
        raise ValueError('output_parent_missing')
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            result = rerank(results, args.query, allow_model_input=args.allow_model_input,
                            offline=args.offline, source_url=getattr(args, 'source_url', DEFAULT_SOURCE_URL))
            stream.write(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(out)
        except OSError:
            pass
        raise
    status = result['decision']['outcome']
    print(json.dumps({'status': status, 'candidates': result['projection']['candidate_count'],
                      'applied': False, 'shadow_only': True}))
    return 0 if status in ('ok', 'abstained') or (args.offline and status == 'unavailable') else 1


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Shadow Jev re-rank annotation over recalled NEURAL_MESH candidates')
    parser.add_argument('--recall', required=True, help='recall_with_proofs output JSON (dict with "results" or a list)')
    parser.add_argument('--query', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--source-url', default=DEFAULT_SOURCE_URL,
                        help='operator-declared public provenance label (system identity, not memory location)')
    parser.add_argument('--allow-model-input', action='store_true')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args(argv)
    return run_cli(args)


if __name__ == '__main__':
    raise SystemExit(main())
