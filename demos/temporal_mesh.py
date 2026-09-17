"""v0.35.0 "Temporal Mesh" demo — bi-temporal recall + reality reconciliation.

Two capabilities, zero deps, deterministic:

  1. BI-TEMPORAL RECALL — supersede a fact, then ask "what did we believe the
     night before?" and "what do we believe now?" and get DIFFERENT answers
     from the SAME store. Flat vector stores cannot do this.

  2. REALITY RECONCILIATION — a mesh believes a wallet holds >= 100 wei; the
     chain says 5. The gate VETOES the action instead of acting on stale memory.

Run:  PYTHONPATH=. python3 demos/temporal_mesh.py
"""
import time

from neural_mesh import Mesh, snapshot, recall_asof, history, resolve_at
from neural_mesh.reconcile import ReconcileGate


def temporal_recall():
    m = Mesh(db_path=":memory:")
    m.add("ROLE: the mesh's chief ambassador is ALICE")
    time.sleep(0.02)
    alice = [n for n in m._load().values() if "ALICE" in n.content][0]
    m.add("ROLE: the mesh's chief ambassador is BOB", supersedes=alice.id)

    t_mid = m._load()[alice.id].valid_to - 0.005
    before = recall_asof(m, "who is the chief ambassador", as_of=t_mid, top_k=1)
    after = recall_asof(m, "who is the chief ambassador", as_of=time.time(), top_k=1)

    print("== BI-TEMPORAL RECALL ==")
    print(f"  before the handoff: {before[0].content}")
    print(f"  after the handoff : {after[0].content}")

    print("  -- version timeline (history) --")
    for step in history(m, alice.id):
        vt = "open" if not step["valid_to"] else f"{step['valid_to']:.3f}"
        print(f"     {step['content'][:40]:42} valid {step['valid_from']:.3f} -> {vt}")


def reality_reconcile():
    # Hermetic chain view: the real balance is 5 wei.
    def fetcher(subject, addr="", data=""):
        return {"eth_balance": 5}.get(subject)

    m = Mesh(db_path=":memory:")
    # The mesh *believes* the wallet is funded — stale/optimistic memory.
    m.add("wallet 0xAA holds >= 100 wei", trust=0.9)

    gate = ReconcileGate(m, fetcher=fetcher, fail_open=True)
    report = gate.reconcile([{
        "subject": "eth_balance", "address": "0xAA",
        "operator": ">=", "value": 100,
        "fact": "wallet holds >= 100 wei (mesh believes this)"}])

    print("\n== REALITY RECONCILIATION ==")
    print(f"  mesh belief: wallet >= 100 wei  (trust 0.9)")
    print(f"  chain truth : 5 wei")
    print(f"  verdict     : {report.verdicts[0].code.upper()}")
    print(f"  action      : {'ALLOWED' if report.allow else 'VETOED (memory contradicted chain)'}")


if __name__ == "__main__":
    temporal_recall()
    reality_reconcile()