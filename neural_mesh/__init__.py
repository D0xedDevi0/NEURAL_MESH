"""NEURAL_MESH — a self-organizing, self-forgetting agentic memory mesh.

Pure-stdlib core (no pip installs required to run the demo).

The mesh models memory the way cognition actually works:
  * FIVE memory types with separate handling (CoALA / 2026 survey)
  * A MESH TOPOLOGY where nodes self-link by meaning (HippoRAG hippocampal indexing)
  * RESONANCE retrieval: a query seeds nodes, activation spreads to linked
    neighbours with decay — the differentiator vs flat cosine search
  * LANES: short-term HOT vs long-term COLD, bridged by a consolidation bus
  * POINTER protocol: big tool outputs never enter context (only a pointer does)
  * SLEEP cycle: replay -> strengthen -> PRUNE weak/aged/low-trust traces
  * PROSPECTIVE memory: intentions & futures, not just the past

Run `python -m neural_mesh.demo` to see it work.
"""

from .core import Mesh, MemoryType
from .meshfile import export_mesh, import_mesh
from .sharing import merge_peer_mesh, consensus_rank, PeerPolicy, export_for_peer
from .lora_dataset import write_jsonl, write_hf_jsonl, write_weights, summarize
from .dream import dream, recall_associative
from .prospective import (
    upcoming, due_rank, snooze, expired,
)
from .budget import select_fit, token_estimate, default_value_score, fit_summary
from .reader import Reader, ExtractiveReader, CallableReader
from .lifecycle import MemoryLifecycle
from .pointer import PointerStore, store_big_output
from .integrations.helixa_provenance import (
    HelixaStamp, stamp_node, verify_stamp, aura_trust_weight,
    export_manifest, make_stamp,
)
from .onchain_provenance import (
    IntuitionDeploymentReceipt, IntuitionTripleReceipt,
    parse_intuition_receipts, load_intuition_receipts,
    receipt_memory_payloads, ingest_intuition_receipts,
)
from .proof_cards import (
    proof_card, node_card, recall_with_proofs,
    answer_with_proofs, citation_for_proof,
)
from .server_security import RateLimiter, auth_ok, origin_allowed, safe_path
from .reader_llm import LLMReader
from .eval import QAJudge, run_qa_eval, load_test_set
# Lazy imports for optional heavy deps — only loaded when actually used,
# so `pip install neural-mesh` works without eth-account / yantrikdb.
_LAZY = {}

def __getattr__(name):
    if name == "HelixaSigner":
        if "HelixaSigner" not in _LAZY:
            from .integrations.helixa_signer import HelixaSigner as _HS
            _LAZY["HelixaSigner"] = _HS
        return _LAZY["HelixaSigner"]
    if name == "YantrikDBBridge":
        if "YantrikDBBridge" not in _LAZY:
            from .integrations.yantrikdb_bridge import YantrikDBBridge as _YB
            _LAZY["YantrikDBBridge"] = _YB
        return _LAZY["YantrikDBBridge"]
    if name == "PaidRecallGate":
        if "PaidRecallGate" not in _LAZY:
            from .x402_recall import PaidRecallGate as _PRG
            _LAZY["PaidRecallGate"] = _PRG
        return _LAZY["PaidRecallGate"]
    raise AttributeError(f"module 'neural_mesh' has no attribute {name!r}")

__all__ = ["Mesh", "MemoryType", "export_mesh", "import_mesh",
           "merge_peer_mesh", "consensus_rank", "PeerPolicy", "export_for_peer",
           "write_jsonl", "write_hf_jsonl", "write_weights", "summarize",
           "dream", "recall_associative", "Reader", "ExtractiveReader",
           "CallableReader", "MemoryLifecycle", "PointerStore", "store_big_output",
           "HelixaStamp", "stamp_node", "verify_stamp", "aura_trust_weight",
           "export_manifest", "make_stamp",
           "IntuitionDeploymentReceipt", "IntuitionTripleReceipt",
           "parse_intuition_receipts", "load_intuition_receipts",
           "receipt_memory_payloads", "ingest_intuition_receipts",
           "proof_card", "node_card", "recall_with_proofs",
           "answer_with_proofs", "citation_for_proof",
           "RateLimiter", "auth_ok", "origin_allowed", "safe_path",
           "LLMReader", "QAJudge", "run_qa_eval", "load_test_set",
           "HelixaSigner",
           "PaidRecallGate", "TIERS", "SERVICE_NAME", "verify_receipt_onchain",
           "RECEIPT_CONTRACT", "FEE_RECIPIENT", "BASE_RPC"]
__version__ = "0.31.0"
