"""
NEURAL_MESH MCP Server — Flask REST API wrapper.
Drop-in template: copy this file to the NEURAL_MESH repo root and run.

Usage:
  cd /opt/data/NEURAL_MESH
  python3 -m venv .venv-server && .venv-server/bin/pip install flask
  .venv-server/bin/python server.py   # listens on :4021

Health check: curl http://localhost:4021/health
"""

import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, request, jsonify, send_from_directory
from neural_mesh.core import Mesh, MemoryType
from neural_mesh import __version__
from neural_mesh.lifecycle import MemoryLifecycle
from neural_mesh.server_security import RateLimiter, auth_ok, origin_allowed, safe_path

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("NEURAL_MESH_MAX_JSON_BYTES", "1048576"))

API_TOKEN = os.environ.get("NEURAL_MESH_API_TOKEN", "")
SAFE_IO_DIR = os.environ.get("NEURAL_MESH_SAFE_IO_DIR", os.path.join(os.path.dirname(__file__), "runtime"))
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("NEURAL_MESH_CORS_ORIGINS", "").split(",") if o.strip()}
RATE_LIMITER = RateLimiter(
    limit=int(os.environ.get("NEURAL_MESH_RATE_LIMIT", "120")),
    window_seconds=int(os.environ.get("NEURAL_MESH_RATE_WINDOW", "60")),
)
AUTH_ENDPOINTS = {"add", "memory_cycle", "sleep_mesh", "consolidate_mesh",
                  "pointer_put", "pointer_summary", "dream", "export_mesh",
                  "merge", "stamp", "intuition_ingest_receipts", "eval_qa",
                  "yantrikdb_ingest", "yantrikdb_think", "helixa_attest_node",
                  "peer_query", "mesh_audit", "federated_recall", "federated_dream", "federation_sync"}
POLICY_FIELDS = {"trust", "cap_trust", "allow_new", "allow_merge"}


def _json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _tool_meta(tool: str, origin: str = "server") -> dict:
    """Provenance stamp for tool-call chains — which tool was used to add
    a node and from where. Surfaces in node.meta['tool_call'] for audit."""
    import time
    return {"tool_call": {"tool": tool, "origin": origin, "ts": time.time()}}


@app.before_request
def harden_request():
    if not RATE_LIMITER.allow(request.remote_addr or "local"):
        return _json_error("rate limit exceeded", 429)
    if request.method in {"POST", "PUT", "PATCH"} and request.is_json is False:
        return _json_error("JSON body required", 415)
    if request.endpoint in AUTH_ENDPOINTS and not auth_ok(request.headers, API_TOKEN):
        return _json_error("authorization required", 401)
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGINS and not origin_allowed(origin, ALLOWED_ORIGINS):
        return _json_error("origin not allowed", 403)


@app.after_request
def harden_response(resp):
    origin = request.headers.get("Origin", "")
    if ALLOWED_ORIGINS and origin_allowed(origin, ALLOWED_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp

# Persist to a file so data survives restarts.
# Set check_same_thread=False because Flask's dev server uses threads.
DB_PATH = os.environ.get("NEURAL_MESH_DB", os.path.join(os.path.dirname(__file__), "mesh.db"))
mesh = Mesh(
    db_path=DB_PATH,
    resonance_backend=os.environ.get("NEURAL_MESH_RESONANCE_BACKEND", "auto"),
)
mesh.db = sqlite3.connect(DB_PATH, check_same_thread=False)  # Overwrite with thread-safe connection
mesh.db.row_factory = sqlite3.Row  # Critical: Mesh._load() indexes rows by column name
POINTER_ROOT = os.environ.get(
    "NEURAL_MESH_POINTER_ROOT",
    os.path.join(os.path.dirname(__file__), "runtime", "pointers"),
)
lifecycle = MemoryLifecycle(
    mesh,
    pointer_root=POINTER_ROOT,
    pointer_threshold=int(os.environ.get("NEURAL_MESH_POINTER_THRESHOLD", "8192")),
)

# ─── Health ────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    # Count nodes from the thread-safe db connection
    cur = mesh.db.execute("SELECT COUNT(*) FROM nodes")
    count = cur.fetchone()[0]
    return jsonify({
        "status": "ok",
        "nodes": count,
        "version": "0.32.0",
        "resonance_backend": mesh.stats()["resonance_backend"],
    })

# ─── Dashboard ─────────────────────────────────────────────────────────────

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Serve the public mesh dashboard."""
    return send_from_directory("static", "dashboard.html")

@app.route("/brain", methods=["GET"])
def brain():
    """Serve the 3D brain visualization."""
    return send_from_directory("static", "brain.html")

@app.route("/brain/og.png", methods=["GET"])
def brain_og():
    """Serve the brain OpenGraph card."""
    return send_from_directory("static", "brain-og.png")

@app.route("/favicon.ico", methods=["GET"])
def favicon():
    """Serve the favicon."""
    return send_from_directory("static", "favicon.png")

@app.route("/brain/favicon.png", methods=["GET"])
def brain_favicon():
    """Serve the favicon under /brain too (import-map asset path)."""
    return send_from_directory("static", "favicon.png")

@app.route("/brain/walk", methods=["POST"])
def brain_walk():
    """Associative walk from a seed node — graph traversal for brain animation.

    Body: {node_id, hops?=2, top_n?=15, mode?="bfs"|"resonance"}
    mode="bfs" (default): BFS through mesh links, ordered by hop distance.
    mode="resonance": spreading activation from the seed (activation decays
        through links); returns nodes ranked by activation with a normalized
        `activation` field (seed=1.0).
    Public read-only — the brain visualization calls this on node click.
    """
    data = request.get_json() or {}
    node_id = data.get("node_id", "")
    if not node_id:
        return _json_error("node_id required", 400)

    hops = max(1, min(int(data.get("hops", 2)), 4))
    top_n = max(1, min(int(data.get("top_n", 15)), 30))
    mode = data.get("mode", "bfs")
    if mode not in {"bfs", "resonance"}:
        return _json_error("mode must be 'bfs' or 'resonance'", 400)

    all_nodes = mesh._load()
    seed = all_nodes.get(node_id)
    if not seed:
        return _json_error("node not found", 404)

    if mode == "resonance":
        # Spreading activation from the seed node (decay=0.5 per hop).
        # Reuse the resonance module's exact propagation contract.
        from neural_mesh.resonance import _spread_python
        resonance = {node_id: 1.0}
        resonance = _spread_python(all_nodes, resonance, [seed], hops, 0.5)
        ranked = sorted(
            ((nid, act) for nid, act in resonance.items() if nid != node_id),
            key=lambda x: -x[1],
        )[:top_n]
        path = [{"node_id": node_id, "distance": 0,
                 "content": seed.content[:140],
                 "lane": seed.lane, "trust": round(seed.trust, 3),
                 "activation": 1.0}]
        for nid, act in ranked:
            n = all_nodes[nid]
            path.append({"node_id": nid, "distance": hops,  # unknown BFS depth in resonance mode
                         "content": n.content[:140],
                         "lane": n.lane, "trust": round(n.trust, 3),
                         "activation": round(act, 4)})
        return jsonify({
            "seed_id": node_id,
            "path": path,
            "hops": hops,
            "mode": "resonance",
            "total_reachable": len(path),
        })

    visited = {node_id: 0}
    frontier = [seed]
    path = [{"node_id": node_id, "distance": 0,
             "content": seed.content[:140],
             "lane": seed.lane, "trust": round(seed.trust, 3)}]
    total_reachable = 1

    for hop in range(1, hops + 1):
        next_frontier = []
        for node in frontier:
            # Sort links by weight desc so strongest edges are walked first
            for nbr_id, weight in sorted(node.links.items(), key=lambda x: -x[1]):
                if nbr_id not in visited:
                    visited[nbr_id] = hop
                    nbr = all_nodes.get(nbr_id)
                    if nbr and not nbr.superseded_by:
                        path.append({"node_id": nbr_id, "distance": hop,
                                     "content": nbr.content[:140],
                                     "lane": nbr.lane, "trust": round(nbr.trust, 3),
                                     "edge_weight": round(weight, 3)})
                        total_reachable += 1
                        next_frontier.append(nbr)
                        if len(path) >= top_n:
                            break
                if len(path) >= top_n:
                    break
            if len(path) >= top_n:
                break
        frontier = next_frontier
        if not frontier:
            break

    return jsonify({
        "seed_id": node_id,
        "path": path,
        "hops": hops,
        "mode": "bfs",
        "total_reachable": total_reachable,
    })

# ─── CRUD ──────────────────────────────────────────────────────────────────

@app.route("/mesh/add", methods=["POST"])
def add():
    """Body: {content, type, provenance?, supersedes?, meta?, by?}"""
    data = request.get_json()
    # tool-call provenance: every node added via the REST API carries
    # meta["tool_call"] so we can audit "which tool brought this in?"
    extra = _tool_meta("mesh/add")
    user_meta = data.get("meta", {})
    if user_meta:
        extra.update(user_meta)
    node = mesh.add(
        content=data["content"],
        type=MemoryType(data.get("type", "semantic")),
        provenance=data.get("provenance", ""),
        supersedes=data.get("supersedes", ""),
        meta=extra,
        by=data.get("by", ""),
    )
    return jsonify({"id": node.id, "content": node.content, "type": node.type.value})


@app.route("/brain/dream-preview", methods=["POST"])
def brain_dream_preview():
    """Dry-run DREAM consolidation — PUBLIC, read-only, zero writes.

    Simulates the DREAM phases (Drift/Evaluate/Reinforce/Archive/Muse) on
    deep copies of the live mesh nodes without mutating the database.
    Returns affected node IDs and candidate insight texts so the brain
    visualization can animate what WOULD happen during a dream cycle.

    Body (optional): {muse: "template"|false} — defaults to template.
    """
    from neural_mesh.dream import dream_preview
    from neural_mesh.muse import template_muse

    data = request.get_json() or {}
    muse = data.get("muse", "template")
    muse_fn = template_muse if muse == "template" else None

    try:
        result = dream_preview(mesh, muse_fn=muse_fn)
    except Exception:
        return _json_error("dream preview failed", 500)

    return jsonify({
        "drifted_ids": result["drifted_ids"],
        "reinforced_ids": result["reinforced_ids"],
        "archived_ids": result["archived_ids"],
        "insights": result["insights"],
        "muse": muse,
        "note": "dry-run — production mesh NOT mutated"
    })


@app.route("/mesh/recall", methods=["POST"])
def recall():
    """Body: {query, limit?, mode?, enhanced?} — enhanced=true merges YantrikDB results."""
    data = request.get_json()
    mode = data.get("mode", "resonance")
    limit = data.get("limit", 10)
    enhanced = data.get("enhanced", False)

    if mode == "dense":
        nodes = mesh.dense_recall(data["query"], top_k=limit, lane=data.get("lane"))
    elif mode == "lexical":
        nodes = mesh.lexical_recall(data["query"], top_k=limit, lane=data.get("lane"))
    elif mode == "hybrid":
        nodes = mesh.hybrid_recall(data["query"], top_k=limit, alpha=data.get("alpha", 0.9),
                                   lane=data.get("lane"))
    else:
        nodes = mesh.recall(
            data["query"],
            top_k=limit,
            lane=data.get("lane"),
        )

    results = [
        {"id": n.id, "content": n.content, "type": n.type.value,
         "lane": n.lane, "trust": n.trust}
        for n in nodes
    ]

    yantrikdb_hits = []
    yantrikdb_status = "unavailable"
    if enhanced:
        try:
            bridge = _yantrikdb_bridge()
            yantrikdb_hits = bridge.recall(data["query"], top_k=min(limit, 5))
            yantrikdb_status = "ok" if yantrikdb_hits else "empty"
        except Exception:
            yantrikdb_status = "unavailable"

    return jsonify({
        "results": results,
        "enhanced": enhanced,
        "yantrikdb": {"status": yantrikdb_status, "hits": len(yantrikdb_hits),
                      "results": yantrikdb_hits} if enhanced else None,
    })


@app.route("/mesh/recall-paid", methods=["POST"])
def recall_paid():
    """x402 payment-gated recall.

    Headers:
      X-Payment-Proof: tx hash of x402 receipt on Base Mainnet
      X-Recall-Tier: basic|deep|ultra (default: basic)

    Body: {query, top_k?, lane?, alpha?}

    Pricing:
      basic  $0.01 — resonance, ≤10 results
      deep   $0.05 — yantrikdb bridge, ≤50 results, proof cards
      ultra  $0.10 — hybrid + yantrikdb, ≤100 results, proof cards + trust scores
    """
    from neural_mesh.x402_recall import PaidRecallGate

    proof_header = request.headers.get("X-Payment-Proof", "").strip()
    tier = request.headers.get("X-Recall-Tier", "basic").strip().lower()
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()

    if not proof_header:
        return _json_error("X-Payment-Proof header required (x402 receipt tx hash)", 402)
    if not query:
        return _json_error("query is required", 400)

    gate = PaidRecallGate(mesh)
    result = gate.paid_recall(
        query=query,
        tier=tier,
        proof_header=proof_header,
        top_k=data.get("top_k", 10),
        lane=data.get("lane"),
        alpha=data.get("alpha", 0.9),
    )

    if not result.get("ok"):
        return jsonify(result), 402

    return jsonify(result)


@app.route("/mesh/federated/recall", methods=["POST"])
def federated_recall():
    """v0.32.0 — paid federated recall across a configured peer mesh registry.

    Query THIS mesh as a paid federated peer: reputation-gate, x402-verify,
    recall, and return a trust-weighted consensus report. Auth-gated (token).

    Headers:
      X-Payment-Proof: x402 receipt tx hash on Base Mainnet (required)
      X-Recall-Tier: basic|deep|ultra (default: basic)

    Body: {query, top_k?, include_local?}
    """
    from neural_mesh.federation import FederatedRecall

    proof = request.headers.get("X-Payment-Proof", "").strip()
    tier = request.headers.get("X-Recall-Tier", "basic").strip().lower()
    data = request.get_json(silent=True) or {}
    query = data.get("query", "").strip()

    if not proof:
        return _json_error("X-Payment-Proof header required (x402 receipt tx hash)", 402)
    if not query:
        return _json_error("query is required", 400)

    # Configured peers come from env (comma-separated base URLs) + optional rep
    # scores (comma-separated, aligned). Absent → self only (still returns a
    # valid local-consensus report so the endpoint is never a dead end).
    peer_urls = [u for u in
                 os.environ.get("NEURAL_MESH_FEDERATED_PEERS", "").split(",")
                 if u.strip()]
    rep_str = os.environ.get("NEURAL_MESH_FEDERATED_PEER_REPS", "")
    reps = [float(x) for x in rep_str.split(",") if x.strip()] if rep_str else []

    try:
        fed = FederatedRecall(
            mesh,
            min_rep=float(os.environ.get("NEURAL_MESH_FEDERATED_MIN_REP", "50")),
            cap_trust=float(os.environ.get("NEURAL_MESH_FEDERATED_CAP_TRUST", "0.9")),
            dry_run=os.environ.get("NEURAL_MESH_FEDERATED_DRY_RUN", "1") == "1",
        )
        from neural_mesh.peer import PeerClient
        for i, u in enumerate(peer_urls):
            rep = reps[i] if i < len(reps) else None
            try:
                fed.discover(u, token=os.environ.get("NEURAL_MESH_PEER_TOKEN", ""),
                             rep=rep)
            except Exception as e:
                fed.add_peer(PeerClient(u), rep=rep if rep is not None else 0.0)
        report = fed.federated_recall(
            query,
            top_k=int(data.get("top_k", 5)),
            tier=tier,
            include_local=bool(data.get("include_local", True)),
        )
    except Exception as e:
        return _json_error(f"federation error: {e}", 500)

    if not report.get("ok"):
        return jsonify(report), 402
    return jsonify(report)


@app.route("/mesh/federation/sync", methods=["POST"])
def federation_sync():
    """v0.32.0 — run the bidirectional memory-economy reconcile loop.

    Auth-gated. Body: {queries: [...], tier?, top_k?, push?}

    Runs MeshFederation.reconcile(): PULL (discover → rep-gate → x402 pay →
    recall → corroborate) then PUSH (local DREAM insight → gate into each
    peer's commons). Returns the full ledger: corroboration-lift, poison
    quarantined, low-rep refused, nodes written, payments. Peers come from
    NEURAL_MESH_FEDERATED_PEERS env (real PeerClient) — none configured
    returns a self-only ledger.
    """
    from neural_mesh.network import MeshFederation

    data = request.get_json(silent=True) or {}
    queries = data.get("queries") or ["memory"]
    tier = data.get("tier", "basic")
    top_k = int(data.get("top_k", 5))
    push = bool(data.get("push", True))

    peer_urls = [u for u in
                 os.environ.get("NEURAL_MESH_FEDERATED_PEERS", "").split(",")
                 if u.strip()]
    rep_str = os.environ.get("NEURAL_MESH_FEDERATED_PEER_REPS", "")
    reps = [float(x) for x in rep_str.split(",") if x.strip()] if rep_str else []

    try:
        fed = MeshFederation(
            mesh,
            min_rep=float(os.environ.get("NEURAL_MESH_FEDERATED_MIN_REP", "50")),
            cap_trust=float(os.environ.get("NEURAL_MESH_FEDERATED_CAP_TRUST", "0.9")),
            dry_run=os.environ.get("NEURAL_MESH_FEDERATED_DRY_RUN", "1") == "1",
        )
        from neural_mesh.peer import PeerClient
        for i, u in enumerate(peer_urls):
            rep = reps[i] if i < len(reps) else None
            try:
                fed.discover(u, token=os.environ.get("NEURAL_MESH_PEER_TOKEN", ""),
                             rep=rep)
            except Exception:
                fed.add_peer(PeerClient(u), rep=rep if rep is not None else 0.0)
        report = fed.reconcile(queries=queries, tier=tier, top_k=top_k, push=push)
    except Exception as e:
        return _json_error(f"federation sync error: {e}", 500)

    return jsonify(report)


@app.route("/mesh/federated/dream", methods=["POST"])
def federated_dream():
    """v0.32.0 — receive DREAM insight contributions into the commons gate.

    Auth-gated. Body: {contributions: [{content, by?, agent_id?, trust?,
    rep?, source_url?}], min_rep?, writeback?}

    Each contribution is gated: reputation (refuse low-rep/unknown),
    ContentValidator poison scan (malicious → quarantine, never live),
    corroboration (matching local fact → trust bump). Returns per-insight
    verdicts: accepted / corroborated / quarantined / refused.
    """
    from neural_mesh.federated_dream import FederatedDream

    data = request.get_json(silent=True) or {}
    contributions = data.get("contributions") or []
    if not contributions:
        return _json_error("contributions list is required", 400)

    min_rep = float(data.get("min_rep",
                             os.environ.get("NEURAL_MESH_FEDERATED_MIN_REP", "50")))
    cap_trust = float(os.environ.get("NEURAL_MESH_FEDERATED_CAP_TRUST", "0.9"))
    try:
        fd = FederatedDream(mesh, min_rep=min_rep, cap_trust=cap_trust)
        report = fd.receive(contributions, writeback=bool(data.get("writeback", True)))
    except Exception as e:
        return _json_error(f"federated dream error: {e}", 500)

    return jsonify({"ok": True, **report})


@app.route("/mesh/cycle", methods=["POST"])
def memory_cycle():
    """Run pointer-safe ingest → routed recall → lanes → sleep.

    Body: {payload, query, label?, type?, mode?, top_k?, alpha?,
           hot_ttl?, cold_threshold?, prune_below?, max_age_days?}
    """
    data = request.get_json() or {}
    if "payload" not in data or "query" not in data:
        return _json_error("payload and query are required", 400)
    try:
        report = lifecycle.cycle(
            data["payload"],
            query=data["query"],
            label=data.get("label", "data"),
            type=MemoryType(data.get("type", "semantic")),
            provenance=data.get("provenance", ""),
            lane=data.get("lane", "hot"),
            trust=float(data.get("trust", 1.0)),
            summary=data.get("summary", ""),
            meta=data.get("meta"),
            mode=data.get("mode", "fact"),
            top_k=int(data.get("top_k", 5)),
            alpha=float(data.get("alpha", 0.9)),
            hot_ttl=float(data.get("hot_ttl", 86_400.0)),
            cold_threshold=int(data.get("cold_threshold", 3)),
            prune_below=float(data.get("prune_below", 0.05)),
            max_age_days=float(data.get("max_age_days", 30.0)),
            retrieval_lane=data.get("retrieval_lane"),
            maintenance_mode=data.get("maintenance_mode", "sleep"),
        )
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    hits = report["retrieval"]["hits"]
    report["retrieval"]["hits"] = [
        {"id": n.id, "content": n.content, "type": n.type.value,
         "lane": n.lane, "trust": n.trust, "meta": n.meta}
        for n in hits
    ]
    return jsonify(report)


@app.route("/mesh/consolidate", methods=["POST"])
def consolidate_mesh():
    """Promote durable hot memories into the cold lane."""
    data = request.get_json() or {}
    try:
        hot_ttl = float(data.get("hot_ttl", 86_400.0))
        cold_threshold = int(data.get("cold_threshold", 3))
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    before = {n.id: n.lane for n in mesh._load().values() if not n.superseded_by}
    mesh.consolidate(hot_ttl=hot_ttl, cold_threshold=cold_threshold)
    after = {n.id: n.lane for n in mesh._load().values() if not n.superseded_by}
    promoted = sum(1 for nid, lane in before.items()
                   if lane == "hot" and after.get(nid) == "cold")
    return jsonify({"promoted": promoted, "stats": mesh.stats()})


@app.route("/mesh/sleep", methods=["POST"])
def sleep_mesh():
    """Run the lightweight replay/strengthen/prune sleep pass."""
    data = request.get_json() or {}
    try:
        prune_below = float(data.get("prune_below", 0.05))
        max_age_days = float(data.get("max_age_days", 30.0))
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    return jsonify(mesh.sleep(prune_below=prune_below, max_age_days=max_age_days))


@app.route("/mesh/pointer", methods=["POST"])
def pointer_put():
    """Externalize a payload. Returns metadata and pointer, never the payload."""
    data = request.get_json() or {}
    payload = data.get("payload")
    if not isinstance(payload, str):
        return _json_error("payload must be a string", 400)
    pointer = lifecycle.pointers.put(payload, data.get("label", "data"))
    return jsonify({"pointer": pointer, "payload_chars": len(payload)})


@app.route("/mesh/pointer/summary", methods=["POST"])
def pointer_summary():
    """Return a bounded preview for a pointer; raw resolution is not exposed."""
    data = request.get_json() or {}
    pointer = data.get("pointer", "")
    if not isinstance(pointer, str) or not pointer.startswith("mesh://"):
        return _json_error("invalid mesh pointer", 400)
    try:
        max_chars = max(32, min(int(data.get("max_chars", 400)), 4_000))
        summary = lifecycle.pointers.summarize(pointer, max_chars=max_chars)
    except (OSError, ValueError, KeyError):
        return _json_error("pointer not found", 404)
    return jsonify({"pointer": pointer, "summary": summary})

# ─── DREAM ─────────────────────────────────────────────────────────────────

@app.route("/mesh/dream", methods=["POST"])
def dream():
    """Run DREAM consolidation cycle. Body: {muse?: "template"|"llm"|false, options?}.

    Returns actionable report with insights, archived, reinforced counts.
    muse="template" (default) generates rule-based insights from surviving clusters.
    muse="llm" calls an LLM (requires OPENROUTER_API_KEY).
    muse=false skips insight generation.
    """
    data = request.get_json() or {}
    muse_mode = data.get("muse", "template")

    muse_fn = None
    if muse_mode == "template":
        from neural_mesh.muse import template_muse
        muse_fn = template_muse
    elif muse_mode == "llm":
        try:
            from neural_mesh.muse import llm_muse
            # Quick test to see if LLM is reachable
            import os
            if not os.environ.get("OPENROUTER_API_KEY"):
                print("[WARN] LLM muse requested but OPENROUTER_API_KEY not set — falling back to template", flush=True)
            else:
                muse_fn = llm_muse
        except Exception as e:
            print(f"[WARN] LLM muse init failed: {e} — falling back to template", flush=True)

    try:
        hot_ttl = float(data.get("hot_ttl", 86_400.0))
        cold_threshold = int(data.get("cold_threshold", 3))
        prune_below = float(data.get("prune_below", 0.04))
        max_age_days = float(data.get("max_age_days", 30.0))
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    report = lifecycle.maintain(
        hot_ttl=hot_ttl,
        cold_threshold=cold_threshold,
        prune_below=prune_below,
        max_age_days=max_age_days,
        mode="dream",
        muse_fn=muse_fn,
    )
    dream_report = report["dream"]
    dream_report["lanes"] = report["lanes"]
    dream_report["stats"] = report["stats"]
    if muse_mode == "llm" and muse_fn is None:
        dream_report["muse_fallback"] = "template (LLM unavailable)"
    return jsonify(dream_report)

# ─── Sharing ───────────────────────────────────────────────────────────────

@app.route("/mesh/export", methods=["POST"])
def export_mesh():
    """Export mesh to .mesh JSONL. Body: {path?}"""
    data = request.get_json() or {}
    try:
        path = safe_path(SAFE_IO_DIR, data.get("path", "exports/mesh_export.mesh"))
    except ValueError as e:
        return _json_error(str(e), 400)
    from neural_mesh.meshfile import export_mesh as em
    em(mesh, path)
    return jsonify({"path": path, "ok": True})

@app.route("/mesh/merge", methods=["POST"])
def merge():
    """Merge peer mesh. Body: {path, policy?{min_trust, max_nodes, dedup_by_hash}}"""
    data = request.get_json()
    from neural_mesh.sharing import PeerPolicy, merge_peer_mesh
    raw_policy = data.get("policy", {})
    unknown = set(raw_policy) - POLICY_FIELDS
    if unknown:
        return _json_error(f"unknown policy fields: {sorted(unknown)}", 400)
    policy = PeerPolicy(**raw_policy) if raw_policy else None
    try:
        path = safe_path(SAFE_IO_DIR, data["path"])
    except (KeyError, ValueError) as e:
        return _json_error(str(e), 400)
    result = merge_peer_mesh(mesh, path, policy=policy)
    return jsonify({"added": result.get("added", 0), "skipped": result.get("skipped", 0)})

# ─── Helixa Provenance ─────────────────────────────────────────────────────

@app.route("/mesh/stamp", methods=["POST"])
def stamp():
    """Add Helixa provenance stamp. Body: {node_id, agent_id, aura_score?, verified_handle?}"""
    data = request.get_json()
    from neural_mesh.integrations.helixa_provenance import stamp_node, HelixaStamp
    stamp_obj = HelixaStamp(
        agent_id=str(data["agent_id"]),
        aura_score=float(data.get("aura_score", 0.0)),
        source="mcp-server",
        vouched_at=__import__("time").time(),
        verified="verified" if data.get("verified_handle") else "unverified",
    )
    stamped = stamp_node(mesh=mesh, node_id=data["node_id"], stamp=stamp_obj)
    return jsonify({"stamped": stamped, "node_id": data["node_id"]})

# ─── Public Community Mesh ──────────────────────────────────────────────────

@app.route("/mesh/public", methods=["GET"])
def public_mesh():
    """Public read-only feed for community dashboard. No auth, rate-limited.
    Query params: q (search), limit (default 10, max 300)"""
    q = request.args.get("q", "")
    try:
        limit = min(int(request.args.get("limit", 10)), 300)
    except ValueError:
        limit = 10

    cur = mesh.db.execute(
        "SELECT id, content, type, meta FROM nodes ORDER BY id DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()

    results = []
    import json
    for r in rows:
        content = r["content"]
        meta = json.loads(r["meta"]) if r["meta"] else {}
        if meta.get("lane") == "quarantine":
            continue  # quarantine lane is never public
        if q and q.lower() not in content.lower():
            continue
        results.append({
            "id": r["id"],
            "content": content[:500],
            "type": r["type"],
            "provenance": meta.get("provenance", "unknown"),
            "by": meta.get("by", "unknown"),
            "trust": meta.get("trust", 1.0),
            "created_at": meta.get("created_at"),
            "lane": meta.get("lane", "hot"),
            "helixa": meta.get("helixa_stamp", {}).get("agent_id") if meta.get("helixa_stamp") else None,
        })

    return jsonify({
        "total": len(results),
        "limit": limit,
        "query": q or None,
        "results": results,
    })

@app.route("/mesh/stats", methods=["GET"])
def mesh_stats():
    """Public stats for dashboard — node count, types, provenance breakdown."""
    import json
    cur = mesh.db.execute("SELECT COUNT(*) as cnt FROM nodes")
    total = cur.fetchone()["cnt"]
    cur = mesh.db.execute("SELECT meta FROM nodes")
    all_meta = [json.loads(r["meta"]) if r["meta"] else {} for r in cur.fetchall()]
    active = len([m for m in all_meta if not m.get("superseded_by")])
    quarantined = len([m for m in all_meta if m.get("lane") == "quarantine"])

    prov_counts = {}
    for m in all_meta:
        src = m.get("provenance", "unknown")
        prov_counts[src] = prov_counts.get(src, 0) + 1
    provenance_breakdown = sorted(
        [{"source": k, "count": v} for k, v in prov_counts.items()],
        key=lambda x: x["count"], reverse=True
    )[:10]

    return jsonify({
        "total_nodes": total,
        "active_nodes": active,
        "consolidated": total - active,
        "quarantined": quarantined,
        "version": "0.32.0",
        "provenance_breakdown": provenance_breakdown,
    })


# ─── ERC-8004 Agent Identity Manifest ────────────────────── v0.27.0 ──────────

@app.route("/mesh/erc8004/manifest", methods=["GET"])
def erc8004_manifest():
    """Public. ERC-8004 registration file (type=registration-v1).

    This is the canonical metadata for NEURAL_MESH as an on-chain agent.
    It is what the IdentityRegistry tokenURI resolves to — name, description,
    services, supported trust models, x402 support, and registrations.

    The mesh's Helixa agent #5287 is wired into the registrations array.
    Actual on-chain minting is a SEPARATE, key-held, human-GO step:
    ``scripts/erc8004_register.py`` on Base Mainnet IdentityRegistry
    (0x8004A169...).

    See https://eips.ethereum.org/EIPS/eip-8004
    """
    stats = mesh.stats()
    return jsonify({
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": "NEURAL_MESH",
        "description": (
            "Self-organizing typed-graph agentic memory engine. "
            "Cross-agent corroboration, resonance retrieval, DREAM "
            "consolidation, versioned truth, OWASP ASI06 memory poisoning "
            "defenses (ContentValidator + quarantine lane + trust decay). "
            f"{stats['total']} nodes live, {stats.get('quarantined', 0)} quarantined. "
            "Trust scores feed into ERC-8004 Reputation Registry."
        ),
        "image": "https://api.d0xeddev.com/brain/og.png",
        "services": [
            {"name": "web", "endpoint": "https://api.d0xeddev.com/brain"},
            {"name": "MCP", "endpoint": "https://api.d0xeddev.com/"},
            {"name": "mesh-api", "endpoint": "https://api.d0xeddev.com/mesh"},
            {"name": "x402", "endpoint": "https://api.d0xeddev.com/x402/record-receipt"},
        ],
        "x402Support": True,
        "active": True,
        "registrations": [
            {
                "agentId": 5287,
                "agentRegistry": ("eip155:8453:"
                                  "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"),
                "helixaAgentId": 60155,
            },
            {
                "agentId": 63912,
                "agentRegistry": ("eip155:8453:"
                                  "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"),
                "owner": "0x23129c0472172D75bEd1e6dd061301796760Ecd9",
                "txHash": ("0xb95f97e8ebb1d17a5039b4f8865a993a3384e953"
                           "c7475343ca021f0d510d6e56"),
                "mintedAt": "2026-08-18",
                "note": "Standalone NEURAL_MESH identity NFT (distinct from Helixa agent).",
            }
        ],
        "supportedTrust": [
            "reputation",
            "crypto-economic",
            "cross-source-corroboration",
        ],
        "version": "0.32.0",
    })


# ─── Security Audit ───────────────────────────────────────── v0.27.0 ──────────

@app.route("/mesh/audit", methods=["GET"])
def mesh_audit():
    """AUTH. List all quarantined nodes — explicit security audit.
    Returns full content (not truncated) because audit is a privileged path."""
    nodes = mesh.audit_quarantine()
    results = []
    for n in nodes:
        results.append({
            "id": n.id,
            "content": n.content[:1000],
            "type": n.type.value,
            "lane": n.lane,
            "trust": n.trust,
            "provenance": n.provenance,
            "by": n.by,
            "resonance": n.resonance,
            "security": n.meta.get("security", {}),
            "tool_call": n.meta.get("tool_call"),
            "created_at": n.created_at,
        })
    return jsonify({"total": len(results), "quarantined": results})


# ─── ERC-8004 Reputation + Validation Feeds ─────────────── v0.27.0 ──────────

@app.route("/mesh/erc8004/reputation", methods=["GET"])
def erc8004_reputation():
    """Public. ERC-8004-compliant reputation signal for a mesh agent.

    Computes a giveFeedback-ready signal from live mesh trust, corroboration,
    Helixa verification, and quarantine rates. Tag1 controls the primary
    signal dimension (starred, corroborated, poisoned_rate, helixa_verified,
    uptime, reachable).

    Query params: ?agent_id=..., &tag1=starred (default: whole mesh, starred)
    """
    agent_id = request.args.get("agent_id", "")
    tag1 = request.args.get("tag1", "starred")
    if tag1 not in {"starred", "corroborated", "poisoned_rate",
                    "helixa_verified", "uptime", "reachable"}:
        return _json_error(f"unknown tag1: {tag1}", 400)

    from neural_mesh.reputation import feedback_signal
    sig = feedback_signal(mesh, agent_id=agent_id, tag1=tag1)
    return jsonify(sig)


@app.route("/mesh/erc8004/validation/<string:agent_id>", methods=["GET"])
def erc8004_validation(agent_id: str):
    """Public. Candidate Validation Provider summary — "has this agent behaved
    honestly?" based on mesh consensus.

    Returns ERC-8263-anchorable per-node content fingerprints so any validator
    can recompute the honesty score from the evidence.
    """
    from neural_mesh.reputation import validation_summary
    summary = validation_summary(mesh, agent_id=agent_id)
    return jsonify(summary)


# ─── Reader ────────────────────────────────────────────────────────────────

@app.route("/mesh/answer", methods=["POST"])
def answer():
    """Generate answer from retrieved context. Body: {query, context_chunks[], reader?}"""
    data = request.get_json()
    from neural_mesh.reader import ExtractiveReader
    reader = ExtractiveReader()
    answer = reader.answer(data["query"], data["context_chunks"])
    return jsonify({"answer": answer, "method": "extractive_proxy"})

@app.route("/mesh/recall-proof", methods=["POST"])
def recall_proof():
    """Recall memories with compact proof cards next to each hit.

    Body: {query, top_k?, mode?, alpha?}. mode = hybrid|dense|lexical|resonance.
    """
    data = request.get_json() or {}
    from neural_mesh.proof_cards import recall_with_proofs
    return jsonify(recall_with_proofs(
        mesh,
        data.get("query", ""),
        top_k=int(data.get("top_k", 5)),
        mode=data.get("mode", "hybrid"),
        alpha=float(data.get("alpha", 0.5)),
    ))

@app.route("/mesh/answer-proof", methods=["POST"])
def answer_proof():
    """Answer from recalled mesh context and attach supporting proof cards.

    Body: {query, top_k?, mode?, alpha?, reader_mode?}. mode = hybrid|dense|lexical|resonance.
    reader_mode = "extractive" (default) | "llm".
    """
    data = request.get_json() or {}
    from neural_mesh.proof_cards import answer_with_proofs
    reader_mode = data.get("reader_mode", "extractive")
    reader = None
    if reader_mode == "llm":
        try:
            from neural_mesh.reader_llm import LLMReader
            reader = LLMReader()
        except Exception:
            pass  # fall back to extractive
    return jsonify(answer_with_proofs(
        mesh,
        data.get("query", ""),
        top_k=int(data.get("top_k", 5)),
        mode=data.get("mode", "hybrid"),
        alpha=float(data.get("alpha", 0.5)),
        reader=reader,
    ))

# ─── Federation & Peer API ──────────────────────────────────────────────────


@app.route("/mesh/peer/manifest", methods=["GET"])
def peer_manifest():
    """Public peer manifest — how another mesh discovers and trusts this one.

    Returns PeerPolicy-compatible metadata: trust caps, node counts,
    provenance breakdown, active lanes, and capability flags.
    Intended for cross-agent mesh federation: an agent queries this endpoint
    to decide whether to trust and merge from this mesh.
    """
    stats = mesh.stats()
    nodes = mesh._load()

    # provenance breakdown
    prov_counts = {}
    lane_counts = {}
    for n in nodes.values():
        if n.superseded_by:
            continue
        p = n.provenance or "unknown"
        prov_counts[p] = prov_counts.get(p, 0) + 1
        lane_counts[n.lane] = lane_counts.get(n.lane, 0) + 1

    return jsonify({
        "mesh_id": "neural-mesh-1",
        "version": __version__,
        "total_nodes": stats["total"],
        "hot": stats.get("hot", 0),
        "cold": stats.get("cold", 0),
        "provenance_breakdown": stats.get("provenance_breakdown", prov_counts),
        "lane_breakdown": lane_counts,
        "resonance_backend": stats.get("resonance_backend", "python"),
        "policy": {
            "trust": True,
            "cap_trust": 0.9,
            "allow_new": True,
            "allow_merge": True,
        },
        "capabilities": [
            "resonance_recall",
            "dense_recall",
            "lexical_recall",
            "hybrid_recall",
            "dream_consolidation",
            "helixa_provenance",
            "peer_merge",
            "subgraph_query",
            "intuition_export",
            "dream_preview",
            "federated_recall",
            "federated_dream",
            "mesh_federation",
        ],
        "query_endpoint": "/mesh/peer/query",
        "federated_endpoint": "/mesh/federated/recall",
        "federated_dream_endpoint": "/mesh/federated/dream",
        "federation_sync_endpoint": "/mesh/federation/sync",
    })


@app.route("/mesh/peer/query", methods=["POST"])
def peer_query():
    """Cross-agent peer recall — query this mesh as a federated peer.

    Body: {query, top_k?=5, lane?=None, mode?="resonance"}
    Returns PeerPolicy-filtered results with trust, provenance,
    agent_id, and Helixa stamps intact for downstream consensus ranking.

    Auth: API_TOKEN required (cross-agent retrieval is trusted OPERATION).
    """
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    if not query:
        return _json_error("query required", 400)

    top_k = max(1, min(int(data.get("top_k", 5)), 50))
    lane = data.get("lane")  # None = all lanes
    if lane not in (None, "hot", "cold"):
        return _json_error("lane must be 'hot', 'cold', or null", 400)

    mode = data.get("mode", "resonance")
    try:
        if mode == "dense":
            results = mesh.dense_recall(query, top_k=top_k, lane=lane)
        elif mode == "lexical":
            results = mesh.lexical_recall(query, top_k=top_k, lane=lane)
        elif mode == "hybrid":
            results = mesh.hybrid_recall(query, top_k=top_k, alpha=data.get("alpha", 0.9), lane=lane)
        else:
            results = mesh.recall(query, top_k=top_k, lane=lane)
    except Exception:
        return _json_error("recall failed", 500)

    from neural_mesh.sharing import consensus_rank
    ranked = consensus_rank(results)

    return jsonify({
        "query": query,
        "mode": mode,
        "results": [
            {
                "id": n.id,
                "content": n.content[:300],
                "type": n.type.value if hasattr(n.type, "value") else str(n.type),
                "trust": round(n.trust, 4),
                "lane": n.lane,
                "provenance": n.provenance or "unknown",
                "agent_id": getattr(n, "agent_id", "") or "self",
                "by": getattr(n, "by", "self") or "self",
                "conflict_group": getattr(n, "conflict_group", None),
                "helixa": (n.meta.get("helixa_stamp", {}).get("agent_id")
                           if n.meta and n.meta.get("helixa_stamp") else None),
                "created_at": getattr(n, "created_at", None),
            } for n in ranked[:top_k]
        ],
        "total": len(results),
        "returned": min(len(results), top_k),
    })


# ─── Subgraph Query ────────────────────────────────────────────────────────


@app.route("/mesh/subgraph", methods=["POST"])
def subgraph_query():
    """Structured subgraph filter — slice the mesh by lane, provenance, author,
    date range, and trust range.  Returns a focused subset of nodes.

    Body: {lane?, provenance?, by?, since?, trust_min?, trust_max?, limit?=50}
    All filters are optional and combined with AND. Public read-only.
    """
    data = request.get_json() or {}
    nodes = mesh._load()

    by_filter = data.get("by", "").strip() or None
    lane_filter = data.get("lane")
    if lane_filter not in (None, "hot", "cold"):
        return _json_error("lane must be 'hot', 'cold', or null", 400)

    provenance = data.get("provenance", "").strip() or None
    since = data.get("since")  # unix timestamp
    trust_min = data.get("trust_min")
    trust_max = data.get("trust_max")
    limit = max(1, min(int(data.get("limit", 50)), 200))

    if since is not None:
        try:
            since = float(since)
        except (TypeError, ValueError):
            return _json_error("since must be a unix timestamp", 400)
    if trust_min is not None:
        try:
            trust_min = float(trust_min)
        except (TypeError, ValueError):
            return _json_error("trust_min must be a float", 400)
    if trust_max is not None:
        try:
            trust_max = float(trust_max)
        except (TypeError, ValueError):
            return _json_error("trust_max must be a float", 400)

    results = []
    for n in nodes.values():
        if n.superseded_by:
            continue
        if by_filter and getattr(n, "by", "") != by_filter:
            continue
        if lane_filter and n.lane != lane_filter:
            continue
        if provenance and n.provenance != provenance:
            continue
        if since is not None:
            created = getattr(n, "created_at", 0) or 0
            if created < since:
                continue
        if trust_min is not None and n.trust < trust_min:
            continue
        if trust_max is not None and n.trust > trust_max:
            continue
        results.append({
            "id": n.id,
            "content": n.content[:200],
            "type": n.type.value if hasattr(n.type, "value") else str(n.type),
            "trust": round(n.trust, 3),
            "lane": n.lane,
            "provenance": n.provenance or "unknown",
            "by": getattr(n, "by", "") or "unknown",
            "created_at": getattr(n, "created_at", None),
        })
        if len(results) >= limit:
            break

    return jsonify({
        "filters": {"by": by_filter, "lane": lane_filter, "provenance": provenance,
                     "since": since, "trust_min": trust_min, "trust_max": trust_max},
        "results": results,
        "total_matched": len(results),
        "limit": limit,
    })


# ─── Intuition Bridge ────────────────────────────────────────────────────

@app.route("/mesh/intuition/export", methods=["GET"])
def intuition_export():
    """Export NEURAL_MESH as Intuition Knowledge Graph Atoms + Triples."""
    from intuition_bridge import build_intuition_graph
    skills = request.args.get("skills", "")
    skills_list = [s.strip() for s in skills.split(",") if s.strip()] if skills else None
    return jsonify(build_intuition_graph(skills_list))

@app.route("/mesh/intuition/ingest-receipts", methods=["POST"])
def intuition_ingest_receipts():
    """Ingest public Intuition receipt markdown as high-trust mesh memories.

    Body: {path?: string}. Defaults to the local deployment receipt file.
    Idempotent: tx/term-derived conflict groups prevent duplicate proof nodes.
    """
    data = request.get_json() or {}
    default_path = os.path.join(os.path.dirname(__file__), "intuition-client", "INTUITION_DEPLOY_RECEIPTS.md")
    raw_path = data.get("path")
    if raw_path:
        try:
            path = safe_path(SAFE_IO_DIR, raw_path)
        except ValueError as e:
            return _json_error(str(e), 400)
    else:
        path = default_path
    from neural_mesh.onchain_provenance import ingest_intuition_receipts
    return jsonify(ingest_intuition_receipts(mesh, path))

@app.route("/eval/qa", methods=["POST"])
def eval_qa():
    """Evaluate mesh QA performance with an LLM judge.

    Body: {examples: [{query, gold}, ...], judge_model?, top_k?}
    Loads a test set into the mesh, runs recall+answer for each question,
    and scores every answer against ground truth via LLM judge.

    Returns aggregated metrics (mean, median, min, max) plus per-item scores.
    Falls back to simple keyword-overlap scoring when no LLM key is available.
    """
    import json as _json
    data = request.get_json() or {}

    examples = data.get("examples")
    if not examples or not isinstance(examples, list):
        return _json_error("required: {examples: [{query, gold}, ...]}", 400)

    from neural_mesh.eval import QAJudge, run_qa_eval
    judge_model = data.get("judge_model")
    top_k = int(data.get("top_k", 5))

    # Wire up LLM judge if env has a key (same detection as LLMReader)
    judge = QAJudge(model=judge_model) if judge_model else QAJudge()

    test_set = [
        {"query": str(ex.get("query", ex.get("q", ""))),
         "gold": str(ex.get("gold", ex.get("answer", ex.get("a", ""))))}
        for ex in examples
    ]

    try:
        metrics = run_qa_eval(mesh, test_set, judge=judge, top_k=top_k)
        return jsonify(metrics)
    except Exception as exc:
        return _json_error(str(exc), 500)

@app.route("/helixa/signer-status", methods=["GET"])
def helixa_signer_status():
    """Report the Helixa signer status without exposing the key."""
    from neural_mesh.integrations.helixa_signer import HelixaSigner, HELIXA_AGENT_ID
    try:
        signer = HelixaSigner()
        return jsonify({
            "ok": True,
            "degraded": signer.degraded,
            "address": signer.address,
            "agent_id": HELIXA_AGENT_ID,
            "note": ("Signer loaded (dry-run only — eth-account not installed). "
                     "Run dry-run attestation: POST /helixa/attest-node"
                     if signer.degraded
                     else "Signer loaded. "
                          "Use POST /helixa/attest-node for attestation "
                          "(dry_run=true by default)."),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


# ─── YantrikDB Bridge ──────────────────────────────────────────────────

def _yantrikdb_bridge():
    """Lazy-init the bridge (single instance across requests)."""
    from neural_mesh.integrations.yantrikdb_bridge import YantrikDBBridge
    return YantrikDBBridge(
        mesh,
        db_path=os.environ.get("YANTRIKDB_DB_PATH", "/opt/data/yantrikdb/memory.db"),
        namespace=os.environ.get("YANTRIKDB_NAMESPACE", "d0xeddev"),
        top_k=int(os.environ.get("YANTRIKDB_TOP_K", "10")),
    )

@app.route("/yantrikdb/status", methods=["GET"])
def yantrikdb_status():
    """YantrikDB bridge availability + embedded stats."""
    br = _yantrikdb_bridge()
    return jsonify({"available": br.available, "stats": br.stats()})

@app.route("/yantrikdb/ingest", methods=["POST"])
def yantrikdb_ingest():
    """Sync existing mesh nodes into yantrikdb (idempotent)."""
    br = _yantrikdb_bridge()
    data = request.get_json() or {}
    return jsonify(br.ingest_mesh(limit=int(data.get("limit", 1000))))

@app.route("/yantrikdb/contradictions", methods=["GET"])
def yantrikdb_contradictions():
    br = _yantrikdb_bridge()
    return jsonify(br.contradictions())

@app.route("/yantrikdb/gaps", methods=["GET"])
def yantrikdb_gaps():
    br = _yantrikdb_bridge()
    limit = int(request.args.get("limit", 20))
    return jsonify(br.gaps(limit=limit))

@app.route("/yantrikdb/think", methods=["POST"])
def yantrikdb_think():
    """Self-direction pass: consolidate + conflict scan."""
    br = _yantrikdb_bridge()
    return jsonify(br.think())

@app.route("/yantrikdb/recall", methods=["POST"])
def yantrikdb_recall():
    """Explainable recall via yantrikdb (per-hit scoring reasons)."""
    data = request.get_json() or {}
    br = _yantrikdb_bridge()
    return jsonify(br.recall(
        data.get("query", ""),
        top_k=int(data.get("top_k", 10)),
    ))

@app.route("/yantrikdb/enhanced-recall", methods=["POST"])
def yantrikdb_enhanced_recall():
    """Merge mesh hybrid recall + yantrikdb explainable recall."""
    data = request.get_json() or {}
    br = _yantrikdb_bridge()
    return jsonify(br.enhanced_recall(
        data.get("query", ""),
        top_k=int(data.get("top_k", 5)),
        mode=data.get("mode", "hybrid"),
        alpha=float(data.get("alpha", 0.9)),
    ))

@app.route("/yantrikdb/skills/search", methods=["POST"])
def yantrikdb_skills_search():
    data = request.get_json() or {}
    br = _yantrikdb_bridge()
    return jsonify(br.search_skills(
        data.get("query", ""), top_k=int(data.get("top_k", 5)),
    ))

@app.route("/helixa/attest-node", methods=["POST"])
def helixa_attest_node():
    """Sign a mesh node attestation with the live Helixa agent wallet.

    Body: {node_id, dry_run? default=true, aura_score?, broadcast? default=false}

    dry_run=false COMMITS a real signature from the agent wallet.
    broadcast=true (with dry_run=false) ALSO publishes the signed attestation
    to the ERC-8004 registry on Base, recording the real tx_hash.
    The private key is NEVER returned — only the signature + tx hash.
    """
    data = request.get_json() or {}
    node_id = data.get("node_id", "")
    if not node_id:
        return _json_error("required: {node_id}", 400)

    dry_run = data.get("dry_run", True)
    aura_score = float(data.get("aura_score", 0.0))
    broadcast = bool(data.get("broadcast", False))

    from neural_mesh.integrations.helixa_signer import HelixaSigner
    try:
        signer = HelixaSigner()
        result = signer.attest_mesh_node(
            mesh, node_id, dry_run=dry_run, aura_score=aura_score,
            broadcast=broadcast,
        )
        return jsonify(result)
    except Exception as exc:
        return _json_error(str(exc), 500)

# ─── Server ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Source env vars for LLM muse (OpenRouter key)
    env_file = os.path.expanduser("/opt/data/.env.d0xeddev_populated")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    if line.startswith("export "):
                        line = line[7:]
                    key, _, val = line.partition("=")
                    val = val.strip().strip('"').strip("'")
                    if key and val:
                        os.environ[key] = val
    app.run(host="0.0.0.0", port=4021, debug=False)
