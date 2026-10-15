"""Tests for the optional Jev retrieval rerank adapter."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from neural_mesh import jev_rerank as jr

FIXTURE = Path(__file__).parent / 'fixtures' / 'recall-cards.json'


def _cards():
    if FIXTURE.is_file():
        return json.loads(FIXTURE.read_bytes())['results']
    return [
        {'id': 'a1b2c3d4e5f6', 'content': 'The Base L2 token DEVIO migrated to a safer contract on 2026-09-01.',
         'type': 'fact', 'trust': 0.9, 'by': 'agent', 'provenance': 'local', 'lane': 'hot', 'resonance': 0.8,
         'proof': {'proof_type': 'intuition_receipt', 'tx': '0xabc', 'network': 'base', 'block': '123'}},
        {'id': 'f6e5d4c3b2a1', 'content': 'Retry policy uses exponential backoff with 3 attempts.',
         'type': 'fact', 'trust': 0.8, 'by': 'agent', 'provenance': 'local', 'lane': 'hot', 'resonance': 0.7},
    ]


class ProjectionTests(unittest.TestCase):
    def test_projection_is_bounded_and_never_surfaces_embedding_or_meta(self):
        cards = copy.deepcopy(_cards())
        cards[0]['embedding'] = [0.1, 0.2, 0.3]
        cards[0]['meta'] = {'api_key': 'secret', 'url': 'https://internal.local/x'}
        p = jr.project_recall(cards, 'What is the DEVIO token status?')
        self.assertEqual(p['recall_order_preserved'], True)
        self.assertFalse(p['applied'])
        self.assertEqual(len(p['candidates']), 2)
        self.assertEqual(p['candidates'][0]['id'], 'a1b2c3d4e5f6')
        for c in p['candidates']:
            self.assertNotIn('embedding', c)
            self.assertNotIn('meta', c)
            self.assertNotIn('resonance', c)
        self.assertNotIn('https://internal.local', json.dumps(p))
        self.assertNotIn('secret', json.dumps(p))

    def test_empty_recall_projects_to_empty(self):
        p = jr.project_recall([], 'anything')
        self.assertEqual(p['candidates'], [])

    def test_malformed_and_duplicate_and_secret_content(self):
        cards = copy.deepcopy(_cards())
        # secret-shaped content is withheld, candidate stays
        cards[0]['content'] = 'my password is hunter2 and ghp_' + 'A' * 36
        p = jr.project_recall(cards, 'query')
        self.assertTrue(p['candidates'][0].get('content_withheld'))
        self.assertNotIn('excerpt', p['candidates'][0])
        self.assertGreaterEqual(p['withheld_content'], 1)
        # duplicate id hard-fails
        dup = [dict(cards[0]), dict(cards[0])]
        with self.assertRaisesRegex(ValueError, 'duplicate_node_id'):
            jr.project_recall(dup, 'query')
        # malformed id hard-fails
        bad = [dict(cards[0])]
        bad[0]['id'] = 'bad id with spaces!'
        with self.assertRaisesRegex(ValueError, 'node_id'):
            jr.project_recall(bad, 'query')

    def test_query_bounds(self):
        with self.assertRaisesRegex(ValueError, 'query'):
            jr.project_recall(_cards(), '')
        with self.assertRaisesRegex(ValueError, 'query'):
            jr.project_recall(_cards(), 'x' * 2000)


class RerankTests(unittest.TestCase):
    def test_no_inference_without_consent_or_offline_and_order_unchanged(self):
        cards = copy.deepcopy(_cards())

        class Forbidden:
            def evaluate(self, *a, **k):
                raise AssertionError('must not call')

        for flags, outcome in [({}, 'denied'), ({'offline': True}, 'unavailable'),
                               ({'allow_model_input': True, 'offline': True}, 'unavailable')]:
            r = jr.rerank(cards, 'query', engine=Forbidden(), **flags)
            self.assertEqual(r['decision']['outcome'], outcome)
            self.assertIsNone(r['suggested_order'])
            self.assertFalse(r['applied'])
            self.assertTrue(r['production_recall_unchanged'])
        self.assertEqual([c['id'] for c in cards], [c['id'] for c in _cards()])

    def test_real_shared_engine_choice_abstention_and_invalid(self):
        try:
            from architect.decision_engine import DecisionEngine
            from architect.decision_types import ProviderResult
        except ImportError:
            self.skipTest('optional shared Architect boundary')
        cards = copy.deepcopy(_cards())

        class Synthetic:
            identity = 'synthetic-mesh-rerank-test-only'

            def __init__(self, mode):
                self.mode = mode
                self.calls = []

            async def ask(self, body):
                self.calls.append(body)
                assert 'embedding' not in json.dumps(body)
                assert 'meta' not in json.dumps(body)
                assert len(body['questions']) <= 7
                answers = {}
                for name, q in body['questions'].items():
                    if q['type'] == 'score':
                        uniform = self.mode == 'uncertain'
                        answers[name] = {
                            'type': 'score', 'score': 1 if uniform else 2,
                            'confidence': 0 if uniform else 1,
                            'probabilities': {str(i): 1/3 if uniform else (1 if i == 2 else 0) for i in range(3)},
                            'legend': {str(i): v for i, v in enumerate(q['criteria'])},
                        }
                    else:
                        choice = 'outside' if self.mode == 'invalid' else 'a1b2c3d4e5f6'
                        answers[name] = {'type': 'choice', 'choice': choice, 'confidence': 1,
                                         'probabilities': {k: 1 if k == 'a1b2c3d4e5f6' else 0 for k in q['criteria']}}
                return ProviderResult({'model': body['model'], 'answers': answers,
                                       'usage': {'input_tokens': 25, 'output_tokens': 25}})

        for mode, expected, outcome in [('ok', ['a1b2c3d4e5f6'], 'ok'),
                                        ('uncertain', None, 'abstained'),
                                        ('invalid', None, 'invalid_response')]:
            provider = Synthetic(mode)
            r = jr.rerank(cards, 'query', allow_model_input=True, engine=DecisionEngine(provider))
            self.assertEqual(r['suggested_order'], expected)
            self.assertEqual(r['decision']['outcome'], outcome)
            self.assertEqual(len(provider.calls), 1)
            self.assertFalse(r['applied'])


if __name__ == '__main__':
    unittest.main()
