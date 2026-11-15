"""Ledger integrity benchmark — what a tamper-evident log actually buys.

Two arms, same engine, same facts, same honest workload:

  * ``ledger``  — NEURAL_MESH with the provenance ledger on.
  * ``rowhash`` — the strongest flat baseline: every row carries a sha256 of its
                  own content, verified row by row. This is what a store with a
                  checksum column can do, and giving the baseline its best
                  operation is the only way the delta means anything.

Deltas are reported per attack class, so a class the baseline also catches is
credited to the baseline rather than buried in an average. Cost (append
overhead, verification throughput) and the false-positive rate on honest
traffic are measured on the same run.

Reproduce:  PYTHONPATH=. .venv/bin/python bench/ledger_integrity_bench.py
Artifact:   bench/results/ledger_integrity.json
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neural_mesh import Mesh, MemoryType                      # noqa: E402
from neural_mesh.dream import dream                            # noqa: E402
from neural_mesh.ledger import ProvenanceLedger                # noqa: E402

SEED_FACTS = [
    "the deploy key rotates every 90 days",
    "the treasury multisig threshold is 2 of 3",
    "the bridge contract is 0xA11CE",
    "the api rate limit is 60 requests per minute",
    "the staging database lives in us-east-1",
    "the incident channel is #ops",
    "the audit window closes on the 15th",
    "the price feed uses a 5 minute TWAP",
]


# --------------------------------------------------------------------------- #
# the flat baseline: per-row content hash
# --------------------------------------------------------------------------- #
class RowHashStore:
    """A store that checksums each row's content and verifies row by row."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, content TEXT,"
            " meta TEXT, content_hash TEXT)")
        self.conn.commit()
        self._n = 0

    def add(self, content, agent_id="", **kw):
        self._n += 1
        nid = f"r{self._n:06d}"
        meta = {"agent_id": agent_id, "superseded_by": "", "valid_to": 0.0}
        self.conn.execute("REPLACE INTO nodes VALUES (?,?,?,?)",
                          (nid, content, json.dumps(meta),
                           hashlib.sha256(content.strip().encode()).hexdigest()))
        self.conn.commit()
        return nid

    def verify(self) -> dict:
        bad = []
        for row in self.conn.execute("SELECT * FROM nodes"):
            want = hashlib.sha256((row["content"] or "").strip().encode()).hexdigest()
            if want != row["content_hash"]:
                bad.append(row["id"])
        return {"ok": not bad, "bad": bad}

    def close(self):
        self.conn.close()


# --------------------------------------------------------------------------- #
# attacks
# --------------------------------------------------------------------------- #
def _raw(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _attack_edit_content(path, mesh_arm):
    """A stored value is changed in place."""
    conn = _raw(path)
    if mesh_arm:
        nid = conn.execute("SELECT id FROM nodes ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE nodes SET content='the deploy key rotates every 900 days'"
                     " WHERE id=?", (nid,))
    else:
        conn.execute("UPDATE nodes SET content='tampered value'"
                     " WHERE id=(SELECT id FROM nodes ORDER BY id LIMIT 1)")
    conn.commit()
    conn.close()


def _attack_launder_author(path, mesh_arm):
    """Authorship is moved from one agent to another (metadata only)."""
    conn = _raw(path)
    row = conn.execute("SELECT id, meta FROM nodes ORDER BY id LIMIT 1").fetchone()
    meta = json.loads(row["meta"])
    meta["agent_id"] = "attacker"
    conn.execute("UPDATE nodes SET meta=? WHERE id=?", (json.dumps(meta), row["id"]))
    conn.commit()
    conn.close()


def _attack_rewrite_validity(path, mesh_arm):
    """A live fact is made to look as if it had already been superseded."""
    conn = _raw(path)
    row = conn.execute("SELECT id, meta FROM nodes ORDER BY id LIMIT 1").fetchone()
    meta = json.loads(row["meta"])
    meta["superseded_by"] = "ghost0000000"
    meta["valid_to"] = time.time() - 86400
    conn.execute("UPDATE nodes SET meta=? WHERE id=?", (json.dumps(meta), row["id"]))
    conn.commit()
    conn.close()


def _attack_delete_row(path, mesh_arm):
    """An inconvenient memory disappears from the store."""
    conn = _raw(path)
    conn.execute("DELETE FROM nodes WHERE id=(SELECT id FROM nodes ORDER BY id DESC LIMIT 1)")
    conn.commit()
    conn.close()


def _attack_insert_direct(path, mesh_arm):
    """A memory is planted behind the API."""
    conn = _raw(path)
    meta = {"created_at": time.time(), "last_accessed": time.time(), "access_count": 0,
            "resonance": 1.0, "trust": 1.0, "provenance": "", "by": "self", "lane": "hot",
            "version": 1, "superseded_by": "", "agent_id": "", "conflict_group": "",
            "valid_from": time.time(), "valid_to": 0.0, "meta": {}}
    if mesh_arm:
        conn.execute("REPLACE INTO nodes VALUES (?,?,?,?,?,?)",
                     ("planted00001", "semantic", "planted behind the api", "[]", "{}",
                      json.dumps(meta)))
    else:
        conn.execute("REPLACE INTO nodes VALUES (?,?,?,?)",
                     ("planted00001", "planted behind the api", json.dumps(meta),
                      hashlib.sha256(b"planted behind the api").hexdigest()))
    conn.commit()
    conn.close()


def _attack_truncate_tail(path, mesh_arm, anchored: bool, wipe_anchor: bool = False):
    """History is cut back to an earlier prefix.

    The strong version: after cutting the log, the store is *repaired* to match
    it — memories whose only witness was in the cut range are removed too. What
    survives is a smaller, perfectly self-consistent store, which is exactly the
    attack a hash chain cannot see locally and an anchor can.

    `wipe_anchor` models the attacker everyone should assume: one with write access
    to the database file. The anchor row lives in that same file, so they delete it
    with the entries it protects. An anchor that survives this must live somewhere
    else — which is the whole point of `anchor_file`.
    """
    conn = _raw(path)
    if mesh_arm:
        n = conn.execute("SELECT COUNT(*) c FROM ledger").fetchone()["c"]
        cut = max(1, n - 2)
        # only memories whose FIRST witness falls in the cut range: keeping a node
        # with an earlier surviving entry would leave an orphaned witness, which is a
        # detectably sloppier attack than the clean truncation modelled here
        orphaned = [r["node_id"] for r in conn.execute(
            "SELECT node_id FROM ledger WHERE node_id != '' GROUP BY node_id "
            "HAVING MIN(seq) >= ?", (cut,)).fetchall()]
        # memories whose supersession is cut away but whose first witness survives:
        # the row must be reverted too, or the surviving entry pins it and the
        # truncation is detected as a rewritten validity window rather than a cut
        reverted = [r["node_id"] for r in conn.execute(
            "SELECT node_id FROM ledger WHERE seq >= ? AND op='supersede' "
            "AND node_id IN (SELECT node_id FROM ledger GROUP BY node_id "
            "HAVING MIN(seq) < ?)", (cut, cut)).fetchall()]
        conn.execute("DELETE FROM ledger WHERE seq >= ?", (cut,))
        if wipe_anchor:
            conn.execute("DELETE FROM ledger_anchor")
        for nid in orphaned:
            conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
        for nid in reverted:
            row = conn.execute("SELECT meta FROM nodes WHERE id=?", (nid,)).fetchone()
            if row is None:
                continue
            meta = json.loads(row["meta"])
            meta["superseded_by"] = ""
            meta["valid_to"] = 0.0
            conn.execute("UPDATE nodes SET meta=? WHERE id=?", (json.dumps(meta), nid))
    else:
        conn.execute("DELETE FROM nodes WHERE id IN (SELECT id FROM nodes ORDER BY id DESC"
                     " LIMIT 2)")
    conn.commit()
    conn.close()


def _attack_edit_log_entry(path, mesh_arm):
    """The audit trail itself is edited (only exists in the ledger arm)."""
    conn = _raw(path)
    conn.execute("UPDATE ledger SET agent_id='someone-else' WHERE seq=2")
    conn.commit()
    conn.close()


ATTACKS = [
    ("content edited in place", "a stored value is rewritten in the row",
     _attack_edit_content, None),
    ("authorship laundered", "agent_id moved to another agent (metadata only)",
     _attack_launder_author, None),
    ("validity rewritten", "a live fact marked already-superseded",
     _attack_rewrite_validity, None),
    ("memory deleted", "an inconvenient row removed from the store",
     _attack_delete_row, None),
    ("memory inserted behind the API", "a row planted with no provenance",
     _attack_insert_direct, None),
    ("tail truncated (unanchored)", "the last entries cut off, prefix intact",
     lambda p, m: _attack_truncate_tail(p, m, anchored=False), None),
    ("tail truncated, in-db anchor wiped",
     "the same cut by an attacker who deletes the anchor row with it",
     lambda p, m: _attack_truncate_tail(p, m, anchored=True, wipe_anchor=True), "anchor"),
    ("tail truncated, anchor in an external file",
     "the same cut against an anchor pinned outside the database",
     lambda p, m: _attack_truncate_tail(p, m, anchored=True, wipe_anchor=True),
     "anchor_file"),
    ("audit trail edited", "a ledger entry rewritten to reframe a write",
     _attack_edit_log_entry, "ledger-only"),
]


# --------------------------------------------------------------------------- #
def _close(store):
    if isinstance(store, RowHashStore):
        store.close()
    else:
        store.db.close()


def _seed_arm(kind: str, path: str, n_facts: int = 24, anchor_file: str = ""):
    """Same facts, same corrections, same workload in both arms."""
    if kind == "rowhash":
        store = RowHashStore(path)
        ids = [store.add(SEED_FACTS[i % len(SEED_FACTS)] + f" #{i}",
                         agent_id="alice" if i % 3 else "self") for i in range(n_facts)]
        return store, ids
    mesh = Mesh(path, validator=False, anchor_file=anchor_file)
    ids = []
    for i in range(n_facts):
        node = mesh.add(SEED_FACTS[i % len(SEED_FACTS)] + f" #{i}",
                        agent_id="alice" if i % 3 else "self")
        ids.append(node.id)
        if i % 6 == 5:
            mesh.add(f"corrected: {SEED_FACTS[i % len(SEED_FACTS)]} #{i}",
                     supersedes=ids[-1], agent_id="self")
    return mesh, ids


def run_attack_experiment(tmp: str) -> list[dict]:
    out = []
    for name, desc, fn, needs in ATTACKS:
        row = {"attack": name, "description": desc}
        for kind in ("rowhash", "ledger"):
            if needs == "ledger-only" and kind == "rowhash":
                row["rowhash"] = "n/a (no audit trail to edit)"
                continue
            path = os.path.join(tmp, f"{kind}-{abs(hash(name)) % 9999}.db")
            sink = os.path.join(tmp, f"{kind}-{abs(hash(name)) % 9999}.anchors.jsonl")
            # the ledger arm always runs with the log on; the external sink is only
            # wired up for the attack that is supposed to be defeated by it
            store, _ids = _seed_arm(kind, path,
                                    anchor_file=sink if needs == "anchor_file" else "")
            if needs in ("anchor", "anchor_file") and kind == "ledger":
                store.ledger.anchor(target="bench")
            fn(path, kind == "ledger")
            if kind == "rowhash":
                verdict = store.verify()
                detected = not verdict["ok"]
                row["rowhash"] = detected
            else:
                rep = store.verify_memory()
                detected = not rep.ok
                row["ledger"] = detected
                row["ledger_kind"] = rep.kind if detected else ""
            _close(store)
        out.append(row)
    return out


def run_false_positive_experiment(tmp: str, n: int = 80) -> dict:
    """Honest traffic only — adds, corrections, bulk ingest, dream, reads.
    A detector that fires here is worse than no detector."""
    path = os.path.join(tmp, "honest.db")
    mesh = Mesh(path, validator=False)
    ids = []
    for i in range(n):
        node = mesh.add(f"honest fact {i} about the mesh workload",
                        agent_id="alice" if i % 4 else "self")
        ids.append(node.id)
        if i % 7 == 3:
            mesh.add(f"honest fact {i} corrected", supersedes=ids[-1])
        if i % 25 == 24:
            mesh.add_many([f"bulk {i}.{j}" for j in range(4)])
        if i % 60 == 59:
            dream(mesh, reinforce_k=3)
            mesh.consolidate(hot_ttl=0.0, cold_threshold=0)
        if i % 11 == 10:
            mesh.recall("honest fact workload", top_k=3)
    # a peer merge: imports are a legitimate path that must be witnessed, not flagged
    peer = Mesh(os.path.join(tmp, "peer.db"), validator=False)
    for i in range(10):
        peer.add(f"peer fact {i}", agent_id="peer-1")
    from neural_mesh.sharing import merge_peer_mesh, PeerPolicy
    export = os.path.join(tmp, "peer_export.json")
    from neural_mesh.sharing import export_for_peer
    export_for_peer(peer, export, agent_id="peer-1")
    merged = merge_peer_mesh(mesh, export, peer_id="peer-1",
                             policy=PeerPolicy(trust=0.6, cap_trust=0.8))
    rep = mesh.verify_memory()
    stats = mesh.ledger.stats()
    result = {
        "operations": n, "ledger_entries": stats["entries"],
        "live_nodes": rep.nodes, "false_positives": 0 if rep.ok else 1,
        "verdict": rep.summary(), "merged_peer_nodes": merged.get("merged", merged),
        "by_op": stats["by_op"],
        "entries_per_node": round(stats["entries"] / max(1, rep.nodes), 3),
    }
    _close(mesh)
    _close(peer)
    return result


def run_cost_experiment(tmp: str, n: int = 120) -> dict:
    """Append overhead against the identical workload with the ledger off."""
    # The two arms are interleaved: one add each, alternating, at the same store size.
    # Run sequentially, container drift (the shared host swings 5%+ between minutes)
    # swamps the effect and has produced both +2.0% and -4.7% for the same code. Both
    # arms must face the same machine at the same n for the delta to mean anything.
    paths = {"ledger_off": os.path.join(tmp, "cost-off.db"),
             "ledger_on": os.path.join(tmp, "cost-on.db")}
    arms = {"ledger_off": Mesh(paths["ledger_off"], validator=False, ledger=False),
            "ledger_on": Mesh(paths["ledger_on"], validator=False, ledger=True)}
    per_add = {name: [] for name in arms}
    for i in range(n):
        for name, mesh in arms.items():
            t0 = time.perf_counter()
            mesh.add(f"cost fact {i} about rotation and thresholds")
            per_add[name].append((time.perf_counter() - t0) * 1e6)  # microseconds

    timings = {}
    for name, mesh in arms.items():
        entries = mesh.ledger.count() if mesh.ledger else 0
        t0 = time.perf_counter()
        rep = mesh.verify_memory() if mesh.ledger else None
        verify_s = time.perf_counter() - t0
        size = os.path.getsize(paths[name])
        timings[name] = {
            "mean_add_us": round(statistics.mean(per_add[name]), 1),
            "p95_add_us": round(sorted(per_add[name])[int(len(per_add[name]) * 0.95)], 1),
            "ledger_entries": entries,
            # keep enough precision that a millisecond-scale verify is not rounded into
            # a fake round number: 40 entries / 0.002s read back as "20000.0 entries/s"
            "verify_seconds": round(verify_s, 6) if rep else None,
            "verify_ms": round(verify_s * 1000, 2) if rep else None,
            "verify_entries_per_second": (round(entries / verify_s) if rep and verify_s
                                          else None),
            "verify_ok": rep.ok if rep else None,
            "db_bytes": size,
        }
        _close(mesh)
    # The ledger's own write cost, isolated from the mesh's O(n^2) add path.
    path = os.path.join(tmp, "cost-raw.db")
    mesh = Mesh(path, validator=False)
    t0 = time.perf_counter()
    n_raw = 2000
    for i in range(n_raw):
        mesh.ledger.append(op="assert", node_id=f"raw{i:06d}", agent_id="alice",
                           digest=f"{i:064x}"[:64], source="bench")
    raw_s = time.perf_counter() - t0
    timings["raw_append"] = {
        "entries": n_raw,
        "seconds": round(raw_s, 3),
        "appends_per_second": round(n_raw / raw_s, 1),
        "us_per_append": round(raw_s / n_raw * 1e6, 1),
    }
    mesh.db.close()

    off, on = timings["ledger_off"], timings["ledger_on"]
    timings["overhead_us_per_add"] = round(on["mean_add_us"] - off["mean_add_us"], 1)
    timings["overhead_pct"] = round(
        100.0 * (on["mean_add_us"] - off["mean_add_us"]) / off["mean_add_us"], 1)
    timings["bytes_per_entry"] = round(
        (on["db_bytes"] - off["db_bytes"]) / max(1, on["ledger_entries"]), 1)
    timings["verify_entries_per_second"] = on.get("verify_entries_per_second")
    return timings


def run_scale_experiment(tmp: str, node_counts=(100, 250)) -> dict:
    """Verification on a store the size of the live mesh (~2.5k nodes)."""
    out = {}
    for n in node_counts:
        path = os.path.join(tmp, f"scale-{n}.db")
        mesh = Mesh(path, validator=False)
        for i in range(n):
            mesh.add(f"scale fact {i} about the mesh at size {n}", agent_id="alice")
        t0 = time.perf_counter()
        rep = mesh.verify_memory()
        elapsed = time.perf_counter() - t0
        out[str(n)] = {"nodes": rep.nodes, "entries": rep.checked, "ok": rep.ok,
                       "verify_ms": round(elapsed * 1000, 1),
                       "nodes_per_second": round(n / elapsed, 1),
                       "db_bytes": os.path.getsize(path)}
        _close(mesh)
    return out


def run_attribution_experiment(tmp: str, n_agents: int = 5, per_agent: int = 20) -> dict:
    """Swarm question: who asserted this? Answered from the log, not the row."""
    path = os.path.join(tmp, "swarm.db")
    mesh = Mesh(path, validator=False)
    truth = {}
    for a in range(n_agents):
        agent = f"agent-{a}"
        for i in range(per_agent):
            node = mesh.add(f"{agent} asserts fact {i}", agent_id=agent)
            truth[node.id] = agent
    correct = 0
    for nid, agent in truth.items():
        p = mesh.provenance_of(nid)
        if agent in p["authors"] and p["witnessed"]:
            correct += 1
    head = mesh.ledger_head()
    # non-repudiation: a peer's bundle proves what it claimed, offline
    bundle = mesh.ledger.export_bundle()
    verified = ProvenanceLedger.verify_bundle(bundle)
    droplet = json.loads(json.dumps(bundle))
    # Frame another agent. Pick a value that actually DIFFERS from the entry's author:
    # assigning the author it already had is a no-op, and the "forgery" then verifies
    # clean — which reads as "forged bundles are accepted" when nothing was forged.
    _target = droplet["entries"][3]
    _original_author = _target.get("agent_id") or ""
    _forged_author = next(
        (f"agent-{i}" for i in range(n_agents) if f"agent-{i}" != _original_author),
        "agent-framed",
    )
    _target["agent_id"] = _forged_author           # frame another agent
    framed = ProvenanceLedger.verify_bundle(droplet)
    if _original_author == _forged_author:         # cannot happen, but never fake a pass
        raise AssertionError("forgery was a no-op: forged author equals the real one")
    _close(mesh)
    return {
        "agents": n_agents, "memories": len(truth),
        "attribution_exact": correct, "attribution_accuracy": round(correct / len(truth), 4),
        "head_seq": head["seq"], "bundle_verified": verified["ok"],
        "bundle_with_forged_author": framed["ok"],
        "bundle_forgery_reason": framed.get("reason", ""),
        "forged_from": _original_author, "forged_to": _forged_author,
        "forgery_was_a_real_change": _original_author != _forged_author,
    }


def main() -> int:
    """Exit codes: 0 ok, 3 the container was too noisy to measure, 4 security regression."""
    tmp = tempfile.mkdtemp(prefix="ledger-bench-")
    results: dict = {"generated_at": time.time(), "scheme": "mesh-ledger-v1",
                     "env": {"loadavg_start": list(os.getloadavg()),
                             "cpu_count": os.cpu_count()}}
    stages = [
        ("attacks", run_attack_experiment),
        ("honest_traffic", run_false_positive_experiment),
        ("cost", run_cost_experiment),
        ("scale", run_scale_experiment),
        ("attribution", run_attribution_experiment),
    ]
    try:
        for name, fn in stages:
            t0 = time.time()
            print(f"[stage] {name} ...", flush=True)
            results[name] = fn(tmp)
            print(f"[stage] {name} done in {time.time() - t0:.1f}s", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("ATTACK DETECTION (row-hash baseline vs provenance ledger)")
    print(f"  {'attack':<34} {'row-hash':<12} ledger")
    for row in results["attacks"]:
        rh = row.get("rowhash")
        rh_s = "n/a" if isinstance(rh, str) else ("caught" if rh else "MISSED")
        lg = row.get("ledger")
        lg_s = "caught" if lg else "MISSED"
        print(f"  {row['attack']:<34} {rh_s:<12} {lg_s}"
              + (f"  [{row.get('ledger_kind','')}]" if lg else ""))
    caught = sum(1 for r in results["attacks"]
                 if r.get("rowhash") is not True and r.get("ledger"))
    print(f"\n  classes the ledger catches that row-hash cannot: {caught}")
    h = results["honest_traffic"]
    print(f"\nHONEST TRAFFIC: {h['operations']} ops, {h['ledger_entries']} entries, "
          f"{h['live_nodes']} live nodes, false positives: {h['false_positives']}")
    c = results["cost"]
    print(f"\nCOST: +{c['overhead_us_per_add']} us/add ({c['overhead_pct']}%), "
          f"{c['bytes_per_entry']} bytes/entry, verify {c['verify_entries_per_second']} entries/s")
    ra = c["raw_append"]
    print(f"      raw ledger append: {ra['appends_per_second']} entries/s "
          f"({ra['us_per_append']} us each), mesh add path excluded")
    print(f"SCALE: " + ", ".join(
        f"{k} nodes -> {v['verify_ms']} ms" for k, v in results["scale"].items()))
    a = results["attribution"]
    # `bundle_with_forged_author` is the bundle's own `ok` flag, so False means the
    # forgery was rejected — print the question, not the raw flag
    print(f"SWARM: {a['attribution_exact']}/{a['memories']} attributed exactly "
          f"({a['attribution_accuracy']}), forged-author bundle rejected: "
          f"{not a['bundle_with_forged_author']} "
          f"({a['bundle_forgery_reason'] or 'no reason given'})")

    results["env"]["loadavg_end"] = list(os.getloadavg())

    # --- refuse to publish numbers the machine contradicts ---------------------
    # A larger store cannot verify faster than a smaller one. When it appears to, the
    # run measured container load, not the ledger — and a committed artifact carrying
    # that number is worse than no artifact, because the next reader will cite it.
    sc = results["scale"]
    if len(sc) >= 2:
        ordered = sorted(sc.items(), key=lambda kv: int(kv[0]))
        (n_small, r_small), (n_big, r_big) = ordered[0], ordered[-1]
        if r_big["verify_ms"] < r_small["verify_ms"]:
            print(f"\nDEGRADED: {n_big} nodes verified faster than {n_small} "
                  f"({r_big['verify_ms']} ms < {r_small['verify_ms']} ms), which is "
                  f"impossible for a store that only grew (load "
                  f"{results['env']['loadavg_end'][:1]}). The run measured the "
                  f"container, not the ledger — refusing to write an artifact.")
            return 3

    # Adding a write to every add cannot make adds faster. A negative overhead is the
    # signature of the sequential-arm bug that produced -4.7 %, so treat it as degraded
    # rather than publishing a physically impossible improvement.
    c = results["cost"]
    if c.get("overhead_us_per_add") is not None and c["overhead_us_per_add"] < 0:
        print(f"\nDEGRADED: ledger overhead measured as "
              f"{c['overhead_us_per_add']} us/add, a negative cost. Recording a ledger "
              f"write cannot speed an add up — refusing to write an artifact.")
        return 3

    attr = results["attribution"]
    if attr.get("forgery_was_a_real_change") and attr.get("bundle_with_forged_author"):
        print("\nSECURITY FAILURE: a bundle with a rewritten author verified OK. "
              "A peer bundle could frame another agent and still pass verification.")
        return 4

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    os.makedirs(out, exist_ok=True)
    artifact = os.path.join(out, "ledger_integrity.json")
    with open(artifact, "w") as fh:
        json.dump(results, fh, indent=2, sort_keys=True)
    print(f"\nartifact: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
