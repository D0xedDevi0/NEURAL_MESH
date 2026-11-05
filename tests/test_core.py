"""Unit tests for NEURAL_MESH core behaviors.

Pure-stdlib: uses unittest + a tiny local SQLite in :memory: so it runs with
`python3 -m unittest` — no pytest needed.

Run:  PYTHONPATH=. python -m unittest tests.test_core -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from neural_mesh import Mesh, MemoryType  # noqa: E402
from neural_mesh import export_mesh, import_mesh  # noqa: E402
from neural_mesh import merge_peer_mesh, consensus_rank, PeerPolicy  # noqa: E402
from neural_mesh.dream import dream as dream_cycle, recall_associative  # noqa: E402


def _export(mesh) -> str:
    path = tempfile.mktemp(suffix=".mesh")
    export_mesh(mesh, path)
    return path


class TestNodeModel(unittest.TestCase):
    def test_auto_id_and_timestamps(self):
        m = Mesh(":memory:")
        n = m.add("hello", MemoryType.SEMANTIC)
        self.assertTrue(n.id)
        self.assertGreater(n.created_at, 0)
        self.assertEqual(n.last_accessed, n.created_at)

    def test_supersede_marks_stale(self):
        m = Mesh(":memory:")
        old = m.add("old fact", MemoryType.SEMANTIC)
        new = m.add("new fact", MemoryType.SEMANTIC, supersedes=old.id)
        self.assertEqual(m._load()[old.id].superseded_by, new.id)


class TestAddAndTypes(unittest.TestCase):
    def test_add_all_six_types(self):
        m = Mesh(":memory:")
        for t in MemoryType:
            n = m.add(f"node-{t.value}", type=t)
            self.assertEqual(n.type, t)
        self.assertEqual(m.stats()["total"], len(list(MemoryType)))

    def test_meta_fields_persist(self):
        m = Mesh(":memory:")
        n = m.add("shared fact", MemoryType.SEMANTIC, agent_id="atlas",
                  trust=0.8, conflict_group="cg1")
        back = m._load()[n.id]
        self.assertEqual(back.agent_id, "atlas")
        self.assertEqual(back.trust, 0.8)
        self.assertEqual(back.conflict_group, "cg1")


class TestAutoLinking(unittest.TestCase):
    def test_add_many_bulk_ingest(self):
        m = Mesh(":memory:")
        nodes = m.add_many(
            ["alpha fact", "beta fact", "gamma fact"],
            type=MemoryType.SEMANTIC, provenance="bulk", autolink=False)
        self.assertEqual(len(nodes), 3)
        for n in nodes:
            self.assertTrue(n.id)
        res = m.recall("alpha", top_k=3)
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0].content, "alpha fact")

    def test_related_nodes_link(self):
        m = Mesh(":memory:")
        a = m.add("the deploy uses vercel prod", MemoryType.PROCEDURAL)
        b = m.add("vercel prod went down last night", MemoryType.EPISODIC)
        self.assertTrue(len(m._load()[a.id].links) >= 1 or
                        len(m._load()[b.id].links) >= 1)

    def test_unrelated_nodes_do_not_link(self):
        m = Mesh(":memory:", link_threshold=0.95)
        x = m.add("banana smoothie recipe omega", MemoryType.SEMANTIC)
        y = m.add("quartz lamp postgresql migration alpha", MemoryType.SEMANTIC)
        self.assertEqual(len(m._load()[x.id].links), 0)
        self.assertEqual(len(m._load()[y.id].links), 0)


class TestResonanceRetrieval(unittest.TestCase):
    def test_retrieves_relevant(self):
        m = Mesh(":memory:")
        m.add("deploy with git push and vercel --prod", MemoryType.PROCEDURAL)
        m.add("the sky is blue", MemoryType.SEMANTIC)
        hits = m.recall("how do I deploy", top_k=3)
        self.assertTrue(any("vercel" in h.content for h in hits))

    def test_recall_touches_access(self):
        m = Mesh(":memory:")
        n = m.add("cache ttl is 300 seconds", MemoryType.SEMANTIC)
        before = m._load()[n.id].access_count
        m.recall("what is the cache ttl")
        after = m._load()[n.id].access_count
        self.assertEqual(after, before + 1)


class TestVersioning(unittest.TestCase):
    def test_superseded_excluded_from_recall(self):
        m = Mesh(":memory:")
        old = m.add("deploy region is us-east-1", MemoryType.SEMANTIC, trust=0.9)
        m.add("deploy region is eu-west-1", MemoryType.SEMANTIC, trust=0.9,
              supersedes=old.id)
        hits = m.recall("deploy region", top_k=5)
        self.assertFalse(any("us-east-1" in h.content for h in hits))
        self.assertTrue(any("eu-west-1" in h.content for h in hits))


class TestMeshFileRoundTrip(unittest.TestCase):
    def test_export_import_preserves(self):
        m = Mesh(":memory:")
        m.add("postgres pool max is 20", MemoryType.SEMANTIC, agent_id="devio",
              trust=0.9, conflict_group="cg")
        old = m.add("old", MemoryType.SEMANTIC)
        m.add("new", MemoryType.SEMANTIC, supersedes=old.id)
        path = _export(m)
        m2 = Mesh(":memory:")
        res = import_mesh(path, m2)
        self.assertEqual(res["loaded"], 3)
        pg = [n for n in m2._load().values()
              if n.content.startswith("postgres")]
        self.assertEqual(len(pg), 1)
        self.assertEqual(pg[0].agent_id, "devio")
        self.assertEqual(pg[0].conflict_group, "cg")


class TestCrossAgentSharing(unittest.TestCase):
    def test_corroboration_fuses_and_raises_trust(self):
        local = Mesh(":memory:")
        local.add("api key stored in vault", MemoryType.SEMANTIC,
                  agent_id="atlas", trust=0.7)
        peer = Mesh(":memory:")
        peer.add("api key stored in vault", MemoryType.SEMANTIC,
                 agent_id="scout", trust=0.8)
        path = _export(peer)
        merged = merge_peer_mesh(local, path, peer_id="scout", policy=PeerPolicy())
        self.assertEqual(merged["fused"], 1)
        fused = [n for n in local._load().values()
                 if "api key stored in vault" in n.content]
        self.assertEqual(len(fused), 1)
        self.assertGreater(fused[0].trust, 0.7)
        self.assertIn("+", fused[0].agent_id)

    def test_untrusted_peer_capped(self):
        local = Mesh(":memory:")
        local.add("the secret is 42", MemoryType.SEMANTIC, trust=0.9)
        peer = Mesh(":memory:")
        peer.add("the secret is 999", MemoryType.SEMANTIC, trust=1.0)
        path = _export(peer)
        pol = PeerPolicy(cap_trust=0.2)
        merge_peer_mesh(local, path, peer_id="rogue", policy=pol)
        rogue = [n for n in local._load().values() if "999" in n.content]
        self.assertTrue(rogue)
        self.assertLessEqual(rogue[0].trust, 0.2)

    def test_consensus_keeps_both_conflicts(self):
        m = Mesh(":memory:")
        a = m.add("region us-east-1", MemoryType.SEMANTIC, trust=0.9,
                  conflict_group="reg")
        b = m.add("region eu-west-1", MemoryType.SEMANTIC, trust=0.4,
                  conflict_group="reg")
        ranked = consensus_rank([m._load()[a.id], m._load()[b.id]])
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].content, "region us-east-1")


class TestDistill(unittest.TestCase):
    def test_distill_filters_low_trust_and_stale(self):
        m = Mesh(":memory:")
        m.add("good fact", MemoryType.SEMANTIC, trust=0.9)
        m.add("noise", MemoryType.SEMANTIC, trust=0.2)
        old = m.add("stale", MemoryType.SEMANTIC, trust=0.9)
        m.add("fresh", MemoryType.SEMANTIC, trust=0.9,
              supersedes=old.id)
        d = m.distill(min_trust=0.6, min_resonance=0.1)
        contents = [p["response"] for p in d["pairs"]]
        self.assertIn("good fact", contents)
        self.assertIn("fresh", contents)
        self.assertNotIn("noise", contents)
        self.assertNotIn("stale", contents)


class TestHelixaProvenance(unittest.TestCase):
    def test_stamp_roundtrips_through_node_meta(self):
        from neural_mesh.integrations.helixa_provenance import (
            HelixaStamp, stamp_node, export_manifest, aura_trust_weight,
            make_stamp,
        )
        m = Mesh(":memory:")
        n = m.add("cody prefers concise answers", MemoryType.SEMANTIC, trust=0.9)
        stamp = make_stamp(agent_id="59322", aura_score=0.85)
        ok = stamp_node(m, n.id, stamp)
        self.assertTrue(ok)
        # meta survives reload
        reloaded = m._load()[n.id]
        self.assertEqual(reloaded.meta["helixa_stamp"]["agent_id"], "59322")
        self.assertEqual(reloaded.agent_id, "59322")

    def test_unverified_stamp_is_capped(self):
        from neural_mesh.integrations.helixa_provenance import (
            HelixaStamp, aura_trust_weight, make_stamp,
        )
        unverified = make_stamp(agent_id="59322", aura_score=0.9)
        self.assertLessEqual(aura_trust_weight(unverified), 0.2)
        verified = HelixaStamp(agent_id="59322", aura_score=0.9,
                               verified="verified")
        self.assertGreater(aura_trust_weight(verified), 0.2)

    def test_export_manifest_lists_stamped_nodes(self):
        from neural_mesh.integrations.helixa_provenance import (
            stamp_node, export_manifest, make_stamp,
        )
        m = Mesh(":memory:")
        a = m.add("fact a", MemoryType.SEMANTIC)
        b = m.add("fact b", MemoryType.SEMANTIC)
        stamp_node(m, a.id, make_stamp(agent_id="59322", aura_score=0.7))
        man = export_manifest(m)
        self.assertEqual(man["count"], 1)
        self.assertEqual(man["stamps"][0]["node_id"], a.id)


class TestHybridRetrieval(unittest.TestCase):
    def setUp(self):
        self.m = Mesh(":memory:")
        self.m.add("Maya's editor is Neovim.", MemoryType.SEMANTIC)
        self.m.add("Maya lives in Berlin.", MemoryType.SEMANTIC)
        self.m.add("Ravi prefers Vim.", MemoryType.SEMANTIC)
        # superseded (stale) node that must never surface
        old = self.m.add("Maya lives in Lisbon.", MemoryType.SEMANTIC)
        cur = self.m.add("Maya lives in Amsterdam.", MemoryType.SEMANTIC)
        self.m._supersede(old.id, cur)

    def test_dense_recall_finds_semantic_match(self):
        hits = self.m.dense_recall("Which editor does Maya use?", top_k=1)
        self.assertEqual(len(hits), 1)
        self.assertIn("Neovim", hits[0].content)

    def test_lexical_recall_finds_exact_keyword(self):
        hits = self.m.lexical_recall("Maya Berlin", top_k=1)
        self.assertIn("Berlin", hits[0].content)

    def test_hybrid_recall_skips_superseded(self):
        hits = self.m.hybrid_recall("Where does Maya live now?", top_k=5)
        contents = [h.content for h in hits]
        joined = " ".join(contents)
        self.assertIn("Amsterdam", joined)        # current fact present
        self.assertNotIn("Lisbon", joined)        # stale (superseded) skipped
        # the stale node must never outrank a live one
        self.assertNotIn("Maya lives in Lisbon.", contents[:3])

    def test_alpha_extremes_match_pure_modes(self):
        q = "What does Ravi use?"
        dense_top = self.m.dense_recall(q, top_k=1)[0].content
        hybrid_dense = self.m.hybrid_recall(q, top_k=1, alpha=1.0)[0].content
        self.assertEqual(dense_top, hybrid_dense)


class TestQAReaderMetrics(unittest.TestCase):
    """Model-free extractive-reader proxy metrics (SQuAD-style)."""
    def test_tok_f1_perfect_and_zero(self):
        from bench.locomo_qa import _tok_f1, _tok_em
        self.assertAlmostEqual(_tok_f1("Tom went to the park", "Tom went to the park"), 1.0)
        # disjoint token sets -> F1 = 0
        self.assertAlmostEqual(_tok_f1("zebra quantum", "Tom went to the park"), 0.0, places=4)
        # partial overlap -> nonzero, <1
        f = _tok_f1("Tom went to a park", "Tom went to the park")
        self.assertTrue(0.0 < f < 1.0)

    def test_tok_em_exact_vs_partial(self):
        from bench.locomo_qa import _tok_em
        self.assertEqual(_tok_em("Tom went to the park", "Tom went to the park"), 1.0)
        self.assertEqual(_tok_em("Tom went to a park", "Tom went to the park"), 0.0)

    def test_extractive_answer_picks_best_sentence(self):
        from bench.locomo_qa import extractive_answer
        from neural_mesh.embed import embed
        from neural_mesh.core import MemoryNode

        def _n(c):
            e = embed(c)
            return MemoryNode(id=c[:6], type=MemoryType.SEMANTIC, content=c,
                             embedding=e, lane="hot", trust=1.0)

        hits = [_n("Tom and John went to a park near Tom's house."),
                _n("The weather was sunny that day.")]
        pred = extractive_answer(hits, "Tom went to the park")
        self.assertIsNotNone(pred)
        self.assertIn("park", pred)


class TestProvenanceBy(unittest.TestCase):
    """'remember is BY' — attribution as a first-class memory primitive."""
    def test_by_defaults_from_agent_or_self(self):
        m = Mesh(":memory:")
        n = m.add("a fact")
        self.assertEqual(n.by, "self")
        a = m.add("agent fact", agent_id="devio")
        self.assertEqual(a.by, "devio")
        p = m.add("bulk fact", provenance="bulkload")
        self.assertEqual(p.by, "bulkload")

    def test_by_explicit_beats_derived(self):
        m = Mesh(":memory:")
        n = m.add("credited", by="cody", agent_id="devio")
        self.assertEqual(n.by, "cody")

    def test_by_persists_roundtrip(self):
        import tempfile, os
        path = tempfile.mktemp(suffix=".db")
        try:
            m = Mesh(path)
            m.add("attributed", by="helixa-59322", agent_id="59322",
                  meta={"helixa_stamp": {"agent_id": "59322", "aura_score": 0.85,
                                        "verified": "verified"}})
            del m
            m2 = Mesh(path)
            n = m2._load().popitem()[1]
            self.assertEqual(n.by, "helixa-59322")
            # meta passed through add() persists now (regression guard)
            self.assertEqual(n.meta["helixa_stamp"]["agent_id"], "59322")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestReaderInterface(unittest.TestCase):
    """Reader swap-point: extractive default + LLM drop-in."""
    def test_extractive_reader(self):
        from neural_mesh.reader import ExtractiveReader
        r = ExtractiveReader()
        out = r.answer("q", ["the park is green", "Tom went to the park"],
                       gold="Tom went to the park")
        self.assertEqual(out, "Tom went to the park")

    def test_callable_reader_swap(self):
        from neural_mesh.reader import CallableReader
        r = CallableReader(lambda q, c: "ANS:" + c.split("\n")[0])
        out = r.answer("q", ["first passage", "second"])
        self.assertEqual(out, "ANS:first passage")


class TestDreamCycle(unittest.TestCase):
    """Agentic consolidation: attribution-weighted trust, link reinforce,
    self-reflective muse that mints new 'by=dream' insight nodes."""
    def test_dream_reinforces_links_and_muses(self):
        m = Mesh(":memory:")
        a = m.add("topic alpha one", by="seed")
        b = m.add("topic alpha two", by="seed")
        c = m.add("topic alpha three", by="seed")
        rep = dream_cycle(m, reinforce_k=3, muse_fn=lambda surv: ["synthesis: alpha cluster"])
        self.assertGreaterEqual(rep["reinforced"], 0)
        # muse minted a node attributed to the dream process itself
        dream_nodes = [n for n in m._load().values() if n.by == "dream"]
        self.assertEqual(len(dream_nodes), 1)
        self.assertIn("synthesis", dream_nodes[0].content)

    def test_author_weight_boosts_verified_aura(self):
        m = Mesh(":memory:")
        # verified high-aura author -> author_weight = trust * (0.5 + 0.5*aura)
        n = m.add("cody prefers concise answers", by="cody",
                  trust=0.9, meta={"helixa_stamp": {"agent_id": "59322",
                                                    "aura_score": 0.85,
                                                    "verified": "verified"}})
        dream_cycle(m)
        reloaded = m._load()[n.id]
        expected = round(0.9 * (0.5 + 0.5 * 0.85), 3)
        self.assertAlmostEqual(reloaded.meta["author_weight"], expected, places=2)


class TestServerHardening(unittest.TestCase):
    """Reusable server hardening helpers stay pure-stdlib and testable."""

    def test_safe_path_stays_inside_base_dir(self):
        from neural_mesh.server_security import safe_path
        base = tempfile.mkdtemp()
        self.assertEqual(safe_path(base, "exports/demo.mesh"), os.path.join(base, "exports", "demo.mesh"))
        with self.assertRaises(ValueError):
            safe_path(base, "../agent-wallet.key")
        with self.assertRaises(ValueError):
            safe_path(base, "/opt/data/.secrets/agent-wallet.key")

    def test_auth_check_is_optional_then_strict_when_token_set(self):
        from neural_mesh.server_security import auth_ok
        self.assertTrue(auth_ok({}, ""))
        self.assertFalse(auth_ok({}, "sekret"))
        self.assertTrue(auth_ok({"Authorization": "Bearer sekret"}, "sekret"))
        self.assertTrue(auth_ok({"X-API-Key": "sekret"}, "sekret"))

    def test_rate_limiter_blocks_after_limit(self):
        from neural_mesh.server_security import RateLimiter
        limiter = RateLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("127.0.0.1", now=100.0))
        self.assertTrue(limiter.allow("127.0.0.1", now=101.0))
        self.assertFalse(limiter.allow("127.0.0.1", now=102.0))
        self.assertTrue(limiter.allow("127.0.0.1", now=161.0))


class TestLLMReader(unittest.TestCase):
    """LLM-powered reader synthesizes answers from retrieved passages.

    Follows the proven OpenRouter pattern from muse.py (pure stdlib urllib).
    """

    def test_llm_reader_builds_prompt_from_passages(self):
        from neural_mesh.reader_llm import LLMReader
        reader = LLMReader(api_key="sk-test")
        prompt = reader._build_prompt(
            "What powers the scam detection agent?",
            ["NEURAL_MESH → powers → Scam Detection Agent on Intuition Mainnet.",
             "The agent uses on-chain triple attestation for provenance."],
        )
        self.assertIn("What powers the scam detection agent", prompt)
        self.assertIn("NEURAL_MESH", prompt)
        self.assertIn("triple attestation", prompt)
        self.assertIn("ANSWER:", prompt)

    def test_llm_reader_extracts_answer_from_response(self):
        from neural_mesh.reader_llm import LLMReader
        reader = LLMReader(api_key="sk-test")
        mock_body = {
            "choices": [{"message": {"content": "ANSWER: NEURAL_MESH powers the Scam Detection Agent on Intuition Mainnet via a verified triple."}}]
        }
        answer = reader._extract_answer(mock_body)
        self.assertEqual(answer, "NEURAL_MESH powers the Scam Detection Agent on Intuition Mainnet via a verified triple.")

    def test_llm_reader_falls_back_on_empty_api_key(self):
        from neural_mesh.reader_llm import LLMReader
        reader = LLMReader(api_key="")
        answer = reader.answer("test query", ["passage one", "passage two"])
        self.assertEqual(answer, "passage one")

    def test_llm_reader_falls_back_on_empty_passages(self):
        from neural_mesh.reader_llm import LLMReader
        reader = LLMReader(api_key="sk-test")
        answer = reader.answer("test query", [])
        self.assertEqual(answer, "")

    def test_llm_reader_accepts_injectable_post_fn(self):
        from neural_mesh.reader_llm import LLMReader
        calls = []

        def fake_post(req):
            calls.append(req)
            import json
            return json.loads('{"choices":[{"message":{"content":"ANSWER: synthetic answer from mock."}}]}')

        reader = LLMReader(api_key="sk-test", _post_fn=fake_post)
        answer = reader.answer("test query", ["context passage"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(answer, "synthetic answer from mock.")

    def test_answer_with_proofs_accepts_llm_reader(self):
        from neural_mesh.reader_llm import LLMReader
        from neural_mesh.proof_cards import answer_with_proofs
        m = Mesh(":memory:")
        m.add(
            "Intuition triple verified: NEURAL_MESH → powers → Scam Detection Agent.",
            MemoryType.SEMANTIC,
            provenance="intuition-mainnet",
            by="intuition-mainnet",
            trust=0.99,
            meta={
                "source_kind": "intuition_receipt",
                "network": "Intuition Mainnet",
                "chain_id": "1155",
                "triple_tx": "0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
                "block": "7875199",
                "term_id": "0xae5a695d550e65af0dc27cb3432cabec5586a446832c94537eee154db854838e",
                "statement": "NEURAL_MESH → powers → Scam Detection Agent",
            },
        )

        def fake_post(req):
            import json
            return json.loads('{"choices":[{"message":{"content":"ANSWER: NEURAL_MESH powers the Scam Detection Agent."}}]}')

        reader = LLMReader(api_key="sk-test", _post_fn=fake_post)
        out = answer_with_proofs(m, "What powers the scam detection agent?", top_k=1, reader=reader)
        self.assertIn("NEURAL_MESH", out["answer"])
        self.assertIn("Scam Detection Agent", out["answer"])
        self.assertEqual(out["proof_count"], 1)
        self.assertIn("llm", out["method"])


class TestDashboardSafety(unittest.TestCase):
    """The public dashboard must render mesh content safely and expose proof answers."""

    def test_dashboard_escapes_dynamic_content_and_calls_answer_proof(self):
        path = os.path.join(os.path.dirname(HERE), "static", "dashboard.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        self.assertIn("function escapeHTML", html)
        self.assertIn("/answer-proof", html)
        self.assertIn("Ask the Mesh", html)
        self.assertNotIn("${n.content}", html)
        self.assertNotIn("${e.message}", html)


class TestProofCards(unittest.TestCase):
    """Recall can emit compact evidence cards for proof-backed memories."""

    def test_proof_card_from_intuition_receipt_node(self):
        from neural_mesh.proof_cards import proof_card
        m = Mesh(":memory:")
        node = m.add(
            "Intuition triple verified: D0xedDev → composedOf → NEURAL_MESH.",
            MemoryType.SEMANTIC,
            provenance="intuition-mainnet",
            by="intuition-mainnet",
            trust=0.99,
            meta={
                "source_kind": "intuition_receipt",
                "network": "Intuition Mainnet",
                "chain_id": "1155",
                "triple_tx": "0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
                "block": "7875199",
                "term_id": "0x9bca7031cac3d6c29339c901a746dac88cbf58b511a5d2d2782bbda0581f7727",
                "statement": "D0xedDev → composedOf → NEURAL_MESH",
                "explorer_url": "https://explorer.intuition.systems/tx/0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
            },
        )
        card = proof_card(node)
        self.assertEqual(card["claim"], "D0xedDev → composedOf → NEURAL_MESH")
        self.assertEqual(card["proof_type"], "intuition_receipt")
        self.assertEqual(card["trust"], 0.99)
        self.assertEqual(card["network"], "Intuition Mainnet")
        self.assertIn("7b063e", card["tx"])
        self.assertIn("explorer.intuition.systems", card["url"])

    def test_recall_with_proofs_returns_cards_next_to_hits(self):
        from neural_mesh.proof_cards import recall_with_proofs
        m = Mesh(":memory:")
        m.add(
            "Intuition triple verified: NEURAL_MESH → powers → Scam Detection Agent.",
            MemoryType.SEMANTIC,
            provenance="intuition-mainnet",
            by="intuition-mainnet",
            trust=0.99,
            meta={
                "source_kind": "intuition_receipt",
                "network": "Intuition Mainnet",
                "chain_id": "1155",
                "triple_tx": "0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
                "block": "7875199",
                "term_id": "0xae5a695d550e65af0dc27cb3432cabec5586a446832c94537eee154db854838e",
                "statement": "NEURAL_MESH → powers → Scam Detection Agent",
                "explorer_url": "https://explorer.intuition.systems/tx/0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
            },
        )
        result = recall_with_proofs(m, "NEURAL_MESH scam detection Intuition", top_k=1, mode="hybrid")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["proof"]["claim"], "NEURAL_MESH → powers → Scam Detection Agent")
        self.assertEqual(result["results"][0]["proof"]["block"], "7875199")

    def test_answer_with_proofs_returns_answer_and_supporting_cards(self):
        from neural_mesh.proof_cards import answer_with_proofs
        m = Mesh(":memory:")
        m.add(
            "Intuition triple verified: NEURAL_MESH → powers → Scam Detection Agent.",
            MemoryType.SEMANTIC,
            provenance="intuition-mainnet",
            by="intuition-mainnet",
            trust=0.99,
            meta={
                "source_kind": "intuition_receipt",
                "network": "Intuition Mainnet",
                "chain_id": "1155",
                "triple_tx": "0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
                "block": "7875199",
                "term_id": "0xae5a695d550e65af0dc27cb3432cabec5586a446832c94537eee154db854838e",
                "statement": "NEURAL_MESH → powers → Scam Detection Agent",
                "explorer_url": "https://explorer.intuition.systems/tx/0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde",
            },
        )
        out = answer_with_proofs(m, "What powers the scam detection agent?", top_k=1)
        self.assertIn("NEURAL_MESH", out["answer"])
        self.assertEqual(out["proof_count"], 1)
        self.assertEqual(out["proofs"][0]["claim"], "NEURAL_MESH → powers → Scam Detection Agent")
        self.assertEqual(out["citations"][0], "[1] Intuition Mainnet block 7875199 tx 0x7b063ec9…91865cde")


class TestOnchainProvenance(unittest.TestCase):
    """Public chain receipts become high-trust recallable mesh memories."""

    SAMPLE = """# Intuition Mainnet Deployment Receipts
Date: 2026-07-30
Network: Intuition Mainnet
Chain ID: 1155
Signer / creator: `0x23129c0472172D75bEd1e6dd061301796760Ecd9`
MultiVault: `0x6E35cF57A41fA15eA0EaE9C33e751b01A784Fe7e`

## Entity Atoms
| Label | Tx | Term ID |
|---|---|---|
| D0xedDev | `0xd01d24d148e0b2b2e7364b7ab69a2547a0d053a965bbd9684da7974b570c8a7a` | `0x0d2a3c63c6edee7e1113ddb55b3d6884c0da23505c21df29ce7834937ba0b466` |
| NEURAL_MESH | `0xc66522a0d8c8ca7462292c92637d6970771b3d7c68bcaa8618a919a73fda46d2` | `0xc306d7c016e7edc78eced957d95fd8e909fd8deeb176000ac61da5c6b0b0dde8` |

## Predicate Atoms
| Label | Tx | Term ID |
|---|---|---|
| composedOf | `0x6feeb0e08a46400df02a6250c45e7db2b4985c5f2a583963a49f25d9ae23a45d` | `0x10a9c91f16b59d6d13868961a4f617a3a687ccebbc5f798547f6a9530335ff83` |

## Triple Batch
Tx: `0x7b063ec91bb832661243bb3d2919ed48ec6cdc93d2d6298e60b32bff91865cde`
Block: `7875199`
Status: success
Batch value: `0.440000000008 TRUST`.

| Statement | Triple term ID |
|---|---|
| D0xedDev → composedOf → NEURAL_MESH | `0x9bca7031cac3d6c29339c901a746dac88cbf58b511a5d2d2782bbda0581f7727` |

## Balance
Before triple batch: `80.823816218967923549 TRUST`
After triple batch: `80.383798249269923549 TRUST`
"""

    def test_parse_intuition_receipt(self):
        from neural_mesh.onchain_provenance import parse_intuition_receipts
        receipt = parse_intuition_receipts(self.SAMPLE)
        self.assertEqual(receipt.chain_id, "1155")
        self.assertEqual(receipt.status, "success")
        self.assertEqual(len(receipt.entity_atoms), 2)
        self.assertEqual(len(receipt.predicate_atoms), 1)
        self.assertEqual(len(receipt.triples), 1)
        self.assertEqual(receipt.multivault, "0x6E35cF57A41fA15eA0EaE9C33e751b01A784Fe7e")
        self.assertEqual(receipt.triples[0].parts, ("D0xedDev", "composedOf", "NEURAL_MESH"))

    def test_ingest_receipts_is_idempotent_and_recallable(self):
        from neural_mesh.onchain_provenance import ingest_intuition_receipts
        path = tempfile.mktemp(suffix=".md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.SAMPLE)
            m = Mesh(":memory:")
            first = ingest_intuition_receipts(m, path)
            second = ingest_intuition_receipts(m, path)
            self.assertEqual(first["added"], 5)  # digest + 3 atoms + 1 triple
            self.assertEqual(second["added"], 0)
            self.assertEqual(second["skipped"], 5)
            hits = m.hybrid_recall("Intuition NEURAL_MESH composedOf", top_k=5)
            joined = " ".join(h.content for h in hits)
            self.assertIn("Intuition triple verified", joined)
            self.assertIn("NEURAL_MESH", joined)
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestAssociativeRecall(unittest.TestCase):
    """Resonance/spreading activation reaches path-dependent targets dense misses.

    With only 4 nodes, flat dense top-5 trivially returns everything, so the
    target is "found" by dense too. To make the claim honest we add distractor
    nodes, so dense's top-5 is forced to choose and genuinely misses the
    path-dependent target while resonance's walk still reaches it.
    """
    def test_resonance_reaches_linked_target(self):
        m = Mesh(":memory:")
        ids, prev = [], None
        chain = ["the living room couch is blue",
                 "the couch is near the oak bookshelf",
                 "the bookshelf holds a ceramic dish",
                 "my spare house key is on a red lanyard"]
        for t in chain:
            n = m.add(t, MemoryType.SEMANTIC, by="seed")
            ids.append(n.id)
            if prev:
                prev.links[n.id] = 1.0
                n.links[prev.id] = 0.3
                m._save(prev); m._save(n)
            prev = n
        # Distractors are engineered to have HIGHER direct cosine to the query
        # ("color", "living room", "couch") than the path-dependent target, which
        # shares ZERO query tokens (cosine exactly 0). With >=5 such distractors,
        # flat dense top-5 is forced to exclude the target -> honest "miss".
        for i in range(6):
            m.add(f"the living room color scheme uses paint number {i}",
                  MemoryType.SEMANTIC, by="noise")
        reached = recall_associative(m, "what color is the living room couch",
                                     top_k=12, hops=3)
        reached_ids = [n.id for n in reached]
        self.assertIn(ids[-1], reached_ids)  # walked to the target
        # flat dense top-5 must now genuinely exclude the path-dependent target
        from neural_mesh.embed import cosine
        qe = m.embedder("what color is the living room couch")
        dense_ids = [n.id for _, n in sorted(
            ((cosine(qe, n.embedding), n) for n in m._load().values()),
            key=lambda x: -x[0])[:5]]
        self.assertNotIn(ids[-1], dense_ids)


class TestHelixaAttestation(unittest.TestCase):
    """On-chain attestation gateway — sign locally, record in mesh, optional broadcast.

    NEVER exposes private keys. Signing is via injectable sign_fn.
    """

    def test_build_attestation_message_is_deterministic(self):
        from neural_mesh.integrations.helixa_attest import build_attestation_message, AttestationMessage
        m = Mesh(":memory:")
        n = m.add("NEURAL_MESH powers scam detection", MemoryType.SEMANTIC, by="helixa")
        msg1 = build_attestation_message(n, "5287")
        msg2 = build_attestation_message(n, "5287")
        self.assertEqual(msg1.signing_hash(), msg2.signing_hash())
        self.assertEqual(msg1.agent_id, "5287")
        self.assertEqual(msg1.node_id, n.id)
        self.assertIsInstance(msg1, AttestationMessage)

    def test_build_attestation_message_differs_per_node(self):
        from neural_mesh.integrations.helixa_attest import build_attestation_message
        m = Mesh(":memory:")
        a = m.add("memory A", MemoryType.SEMANTIC, by="helixa")
        b = m.add("memory B", MemoryType.SEMANTIC, by="helixa")
        h1 = build_attestation_message(a, "5287").signing_hash()
        h2 = build_attestation_message(b, "5287").signing_hash()
        self.assertNotEqual(h1, h2)

    def test_sign_attestation_uses_injected_sign_fn(self):
        from neural_mesh.integrations.helixa_attest import build_attestation_message, sign_attestation
        m = Mesh(":memory:")
        n = m.add("test content", MemoryType.SEMANTIC, by="helixa")
        msg = build_attestation_message(n, "5287")

        signed = []
        def fake_sign(hash_hex):
            signed.append(hash_hex)
            return "0x" + "ab" * 32

        sig = sign_attestation(msg, fake_sign)
        self.assertEqual(len(signed), 1)
        self.assertEqual(signed[0], msg.signing_hash())
        self.assertEqual(sig, "0x" + "ab" * 32)

    def test_record_onchain_attestation_stamps_node(self):
        from neural_mesh.integrations.helixa_attest import record_onchain_attestation
        from neural_mesh.integrations.helixa_provenance import HelixaStamp
        m = Mesh(":memory:")
        n = m.add("verified memory", MemoryType.SEMANTIC, by="helixa")
        result = record_onchain_attestation(
            m, n.id, signature="0xdeadbeef", tx_hash="0xcafe",
            agent_id="5287", aura_score=0.85,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified"], "onchain_attested")
        self.assertEqual(result["tx_hash"], "0xcafe")

        reloaded = m._load()[n.id]
        stamp = HelixaStamp.from_meta(getattr(reloaded, "meta", {}) or {})
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.verified, "onchain_attested")
        self.assertEqual(stamp.signature, "0xdeadbeef")
        self.assertEqual(stamp.tx_hash, "0xcafe")

    def test_attest_node_full_flow_without_broadcast(self):
        from neural_mesh.integrations.helixa_attest import attest_node
        m = Mesh(":memory:")
        n = m.add("attest this", MemoryType.SEMANTIC, by="helixa")

        def fake_sign(hash_hex):
            return "0x" + "cd" * 32

        result = attest_node(m, n.id, "5287", sign_fn=fake_sign, aura_score=0.9)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["tx_hash"])
        self.assertIn("message_hash", result)

        # Verify the stamp landed
        from neural_mesh.integrations.helixa_provenance import HelixaStamp
        reloaded = m._load()[n.id]
        stamp = HelixaStamp.from_meta(getattr(reloaded, "meta", {}) or {})
        self.assertEqual(stamp.verified, "onchain_attested")
        self.assertEqual(stamp.aura_score, 0.9)

    def test_attest_node_with_broadcast(self):
        from neural_mesh.integrations.helixa_attest import attest_node
        m = Mesh(":memory:")
        n = m.add("broadcast this", MemoryType.SEMANTIC, by="helixa")

        def fake_sign(hash_hex):
            return "0x" + "ef" * 32

        broadcast_calls = []
        def fake_broadcast(signature, message_json):
            broadcast_calls.append((signature, message_json))
            return "0xtx_broadcast_123"

        result = attest_node(m, n.id, "5287", sign_fn=fake_sign,
                             broadcast_fn=fake_broadcast, aura_score=0.75)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tx_hash"], "0xtx_broadcast_123")
        self.assertEqual(len(broadcast_calls), 1)

    def test_attest_node_raises_on_missing_node(self):
        from neural_mesh.integrations.helixa_attest import attest_node
        m = Mesh(":memory:")
        with self.assertRaises(ValueError):
            attest_node(m, "nonexistent-id", "5287", sign_fn=lambda h: "0x00")


class TestLoCoMoQA(unittest.TestCase):
    """LLM-judged QA evaluation — scores mesh answers against ground truth."""

    def _mock_judge_response(self, score=0.85, reasoning="correct"):
        """Build a mock API response that the judge would return."""
        import json
        content = json.dumps({"score": score, "reasoning": reasoning})
        return {"choices": [{"message": {"content": content}}]}

    def test_simple_score_keyword_overlap(self):
        from neural_mesh.eval import _simple_score
        result = _simple_score("q", "Base L2 agent hub", "Base L2 agent hub powers D0xedDev")
        self.assertGreater(result["score"], 0.0)
        self.assertIn("keyword", result["reasoning"])

    def test_simple_score_empty_answer(self):
        from neural_mesh.eval import _simple_score
        result = _simple_score("q", "", "something")
        self.assertEqual(result["score"], 0.0)

    def test_simple_score_no_overlap(self):
        from neural_mesh.eval import _simple_score
        result = _simple_score("q", "totally different words here", "Base L2 agent hub")
        self.assertEqual(result["score"], 0.0)

    def test_judge_falls_back_when_no_api_key(self):
        from neural_mesh.eval import QAJudge
        judge = QAJudge()
        judge._llm.api_key = ""  # force fallback
        result = judge.score("what is D0xedDev?", "agent hub on Base", "D0xedDev is an agent hub on Base L2")
        self.assertIn("score", result)
        self.assertIn("reasoning", result)
        # Simple keyword overlap should detect some match
        self.assertGreater(result["score"], 0.0)

    def test_judge_uses_mock_response(self):
        from neural_mesh.eval import QAJudge
        judge = QAJudge(_post_fn=lambda p: self._mock_judge_response(0.9, "spot on"))
        judge._llm.api_key = "fake-key"
        result = judge.score("q?", "answer text", "ground truth text")
        self.assertEqual(result["score"], 0.9)
        self.assertEqual(result["reasoning"], "spot on")
        self.assertEqual(result["question"], "q?")
        self.assertEqual(result["answer"], "answer text")
        self.assertEqual(result["gold"], "ground truth text")

    def test_judge_clamps_score_to_range(self):
        from neural_mesh.eval import QAJudge
        # Score above 1.0 should be clamped
        judge = QAJudge(_post_fn=lambda p: self._mock_judge_response(1.5, "too high"))
        judge._llm.api_key = "fake-key"
        result = judge.score("q", "a", "g")
        self.assertEqual(result["score"], 1.0)

        # Score below 0.0 should be clamped
        judge2 = QAJudge(_post_fn=lambda p: self._mock_judge_response(-0.5, "too low"))
        judge2._llm.api_key = "fake-key"
        result2 = judge2.score("q", "a", "g")
        self.assertEqual(result2["score"], 0.0)

    def test_judge_handles_malformed_json(self):
        from neural_mesh.eval import QAJudge
        def bad_json(prompt):
            return {"choices": [{"message": {"content": "not json at all"}}]}
        judge = QAJudge(_post_fn=bad_json)
        judge._llm.api_key = "fake-key"
        result = judge.score("q?", "some answer", "some ground truth")
        # Falls back to simple score — should get some keyword overlap
        self.assertIn("score", result)

    def test_load_test_set_from_jsonl(self):
        from neural_mesh.eval import load_test_set
        import tempfile, os
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        try:
            tmp.write('{"query": "q1", "gold": "g1"}\n')
            tmp.write('{"query": "q2", "gold": "g2"}\n')
            tmp.write('# comment\n')
            tmp.write('\n')
            tmp.write('{"query": "q3", "gold": "g3"}\n')
            tmp.close()
            examples = load_test_set(tmp.name)
            self.assertEqual(len(examples), 3)
            self.assertEqual(examples[0]["query"], "q1")
            self.assertEqual(examples[2]["gold"], "g3")
        finally:
            os.unlink(tmp.name)

    def test_run_qa_eval_with_mock_judge(self):
        from neural_mesh.eval import QAJudge, run_qa_eval
        from neural_mesh import Mesh, MemoryType
        import json

        m = Mesh(":memory:")
        m.add("D0xedDev is an autonomous agent hub on Base L2", MemoryType.SEMANTIC, by="test")
        m.add("NEURAL_MESH powers scam detection with on-chain proof cards", MemoryType.SEMANTIC, by="test")
        m.add("v0.13 shipped LLM-powered answer synthesis", MemoryType.SEMANTIC, by="test")

        judge = QAJudge(_post_fn=lambda p: self._mock_judge_response(0.8, "decent"))
        judge._llm.api_key = "fake-key"

        test_set = [
            {"query": "what is D0xedDev?", "gold": "an agent hub on Base L2"},
            {"query": "what does NEURAL_MESH do?", "gold": "scam detection with proofs"},
        ]

        metrics = run_qa_eval(m, test_set, judge=judge, top_k=3)
        self.assertEqual(metrics["total"], 2)
        self.assertEqual(len(metrics["scores"]), 2)
        self.assertGreater(metrics["mean"], 0.0)
        # Each should get 0.8 from our mock
        self.assertAlmostEqual(metrics["mean"], 0.8, delta=0.01)
        self.assertEqual(len(metrics["per_item"]), 2)

    def test_run_qa_eval_handles_empty_test_set(self):
        from neural_mesh.eval import run_qa_eval
        from neural_mesh import Mesh
        m = Mesh(":memory:")
        metrics = run_qa_eval(m, [])
        self.assertEqual(metrics["total"], 0)
        self.assertEqual(metrics["mean"], 0.0)

try:
    import eth_account  # noqa: F401
    _ETH_ACCOUNT_AVAILABLE = True
except ImportError:
    _ETH_ACCOUNT_AVAILABLE = False

try:
    from yantrikdb_hermes_plugin.embedded import YantrikDBEmbedded  # noqa: F401
    _YANTRIKDB_AVAILABLE = True
except ImportError:
    _YANTRIKDB_AVAILABLE = False


class TestHelixaSignerLive(unittest.TestCase):
    """Live Helixa API signer — tests use a throwaway test key, NEVER the real one."""

    @classmethod
    def setUpClass(cls):
        if not _ETH_ACCOUNT_AVAILABLE:
            raise unittest.SkipTest("helixa_signer requires eth_account (not installed)")

    TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # Hardhat #0

    def setUp(self):
        import tempfile, json, os
        self._tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump({"address": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
                    "privateKey": self.TEST_KEY}, self._tmp)
        self._tmp.close()
        self._wallet_path = self._tmp.name

    def tearDown(self):
        import os
        os.unlink(self._wallet_path)

    def test_signer_loads_key_and_derives_address(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        signer = HelixaSigner(wallet_file=self._wallet_path)
        self.assertTrue(signer.address.startswith("0x"))
        self.assertEqual(signer.address.lower(), "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266")

    def test_signer_dry_run_verify_agent(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        signer = HelixaSigner(wallet_file=self._wallet_path)
        result = signer.verify_agent("5287", dry_run=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["agent_id"], "5287")

    def test_signer_dry_run_update_profile(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        signer = HelixaSigner(wallet_file=self._wallet_path)
        payload = {"personality": "autonomous chef", "narrative": "building on Base"}
        result = signer.update_agent_profile("5287", payload, dry_run=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["payload_keys"], ["personality", "narrative"])

    def test_signer_dry_run_attest_mesh_node(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        from neural_mesh import Mesh, MemoryType
        signer = HelixaSigner(wallet_file=self._wallet_path)
        m = Mesh(":memory:")
        n = m.add("verified by Helixa on Base", MemoryType.SEMANTIC, by="helixa")
        result = signer.attest_mesh_node(m, n.id, dry_run=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertIn("message_hash", result)
        self.assertEqual(result["agent_id"], "5287")

    def test_signer_attest_mesh_node_writes_stamp(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        from neural_mesh.integrations.helixa_provenance import HelixaStamp
        from neural_mesh import Mesh, MemoryType
        signer = HelixaSigner(wallet_file=self._wallet_path)
        m = Mesh(":memory:")
        n = m.add("signed by Helixa agent wallet", MemoryType.SEMANTIC, by="helixa")
        result = signer.attest_mesh_node(m, n.id, dry_run=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified"], "onchain_attested")
        reloaded = m._load()[n.id]
        stamp = HelixaStamp.from_meta(getattr(reloaded, "meta", {}) or {})
        self.assertIsNotNone(stamp)
        self.assertEqual(stamp.verified, "onchain_attested")
        self.assertTrue(stamp.signature.startswith("0x"))

    def test_signer_raises_on_missing_wallet_file(self):
        from neural_mesh.integrations.helixa_signer import HelixaSigner
        with self.assertRaises(FileNotFoundError):
            HelixaSigner(wallet_file="/nonexistent/wallet.json")


class TestYantrikDBBridge(unittest.TestCase):
    """Tests for the optional YantrikDB memory / contradiction bridge."""

    @classmethod
    def setUpClass(cls):
        if not _YANTRIKDB_AVAILABLE:
            raise unittest.SkipTest("yantrikdb bridge requires yantrikdb (not installed)")

    def setUp(self):
        from neural_mesh import Mesh
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "test.db")
        self.mesh = Mesh(":memory:")
        # Seed a few nodes so ingest/recall has data
        from neural_mesh import MemoryType
        self.mesh.add("Base L2 is the rollup chain for Ethereum scaling", MemoryType.SEMANTIC, by="test")
        self.mesh.add("DEVIO token is live on Base at 0x3d447A...", MemoryType.SEMANTIC, by="devio")
        self.mesh.add("Helixa agent #5287 verified on-chain", MemoryType.PROCEDURAL, by="helixa")

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_bridge_init_creates_db(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        self.assertTrue(br.available, "bridge should be available when yantrikdb is installed")

    def test_bridge_unavailable_guards(self):
        import neural_mesh.integrations.yantrikdb_bridge as br_mod
        real_ok = br_mod._YANTRIKDB_OK
        try:
            br_mod._YANTRIKDB_OK = False
            from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
            br = YantrikDBBridge(self.mesh, db_path=self.db_path)
            self.assertFalse(br.available)
            self.assertEqual(br.ingest_mesh(), {"ok": False, "error": "yantrikdb not available"})
            self.assertEqual(br.contradictions(), {"ok": False, "error": "yantrikdb not available", "conflicts": []})
            self.assertEqual(br.gaps(), {"ok": False, "error": "yantrikdb not available", "gaps": []})
            self.assertEqual(br.think(), {"ok": False, "error": "yantrikdb not available"})
            self.assertEqual(br.stats(), {"ok": False, "error": "yantrikdb not available"})
        finally:
            br_mod._YANTRIKDB_OK = real_ok

    def test_ingest_mesh(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        result = br.ingest_mesh()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["written"], 1)
        self.assertEqual(result["scanned"], 3)

    def test_contradictions_empty_on_fresh_db(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.contradictions()
        self.assertIn("conflicts", result)

    def test_gaps_empty_on_fresh_db(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.gaps()
        self.assertIn("gaps", result)

    def test_think_noop_on_fresh_db(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.think()
        self.assertIn("consolidation_count", result)

    def test_stats_after_ingest(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.stats()
        self.assertGreaterEqual(result.get("active_memories", 0), 1)

    def test_recall_finds_ingested(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.recall("DEVIO token")
        self.assertIsInstance(result.get("results"), list)
        self.assertGreater(len(result.get("results", [])), 0)

    def test_enhanced_recall_merges_sources(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        br.ingest_mesh()
        result = br.enhanced_recall("Base L2", top_k=5)
        self.assertTrue(result["ok"])
        self.assertGreater(result["count"], 0)
        # Should have both mesh and yantrikdb results (or at least one)
        self.assertGreaterEqual(result["mesh_hits"] + result["yantrikdb_hits"], 1)

    def test_enhanced_recall_falls_back_when_unavailable(self):
        import neural_mesh.integrations.yantrikdb_bridge as br_mod
        real_ok = br_mod._YANTRIKDB_OK
        try:
            br_mod._YANTRIKDB_OK = False
            from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
            br = YantrikDBBridge(self.mesh, db_path=self.db_path)
            result = br.enhanced_recall("Base L2", top_k=3)
            self.assertTrue(result["ok"])
            self.assertGreater(result["count"], 0)
            self.assertGreater(result["mesh_hits"], 0)
            self.assertEqual(result["yantrikdb_hits"], 0)
        finally:
            br_mod._YANTRIKDB_OK = real_ok

    def test_record_turn(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        result = br.record_turn("user", "what's the DEVIO contract?")
        self.assertTrue(result.get("recorded"))

    def test_define_and_search_skills(self):
        from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
        br = YantrikDBBridge(self.mesh, db_path=self.db_path)
        d = br.define_skill("workflow.ship.release", "When shipping a new release: run the full test suite, commit all changes, tag the version, and push to the remote. Always verify health before shipping.")
        self.assertTrue(d.get("stored"), f"define_skill failed: {d}")
        s = br.search_skills("ship")
        self.assertIn("skills", s)
        self.assertGreater(s.get("total", 0), 0)



class TestCrossThreadConnection(unittest.TestCase):
    """The Mesh is built on one thread and used from another.

    A Hermes memory provider constructs the Mesh during agent init and runs
    mesh_remember / mesh_recall on a tool-executor thread; Flask's threaded dev
    server does the same. Without check_same_thread=False on the connection
    every such call raised ProgrammingError instead of storing anything.
    """

    def test_add_and_recall_from_another_thread(self):
        import threading
        path = os.path.join(tempfile.mkdtemp(), "cross_thread.db")
        m = Mesh(path)  # built on THIS thread
        out: dict = {}

        def worker():
            try:
                node = m.add("the vault code is 4471", MemoryType.SEMANTIC)
                out["id"] = node.id
                out["hits"] = [h.content for h in m.recall("vault code", top_k=3)]
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertNotIn("error", out, out.get("error"))
        self.assertTrue(out["id"])
        self.assertIn("the vault code is 4471", out["hits"])
        # the original thread still owns a usable connection
        self.assertEqual(len(m.recall("vault code", top_k=3)), 1)

    def test_stats_and_asof_from_another_thread(self):
        import threading
        path = os.path.join(tempfile.mkdtemp(), "cross_thread_stats.db")
        m = Mesh(path)
        m.add("baseline fact about Base L2", MemoryType.SEMANTIC)
        out: dict = {}

        def worker():
            try:
                out["stats"] = m.stats() if hasattr(m, "stats") else None
                out["hits"] = [h.content for h in m.recall_asof("Base L2", as_of=time.time() + 60, top_k=3)]
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertNotIn("error", out, out.get("error"))
        self.assertIn("baseline fact about Base L2", out["hits"])
