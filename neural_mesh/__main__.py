"""NEURAL_MESH CLI — python -m neural_mesh <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def cmd_export(args):
    from neural_mesh import Mesh, export_mesh
    mesh = Mesh(args.db or ":memory:")
    result = export_mesh(mesh, args.output)
    print(json.dumps(result, indent=2))


def cmd_import(args):
    from neural_mesh import Mesh, import_mesh
    mesh = Mesh(args.db or ":memory:")
    result = import_mesh(args.input, mesh, reembed=not args.no_reembed)
    print(json.dumps(result, indent=2))


def cmd_merge(args):
    from neural_mesh import Mesh, merge_peer_mesh, PeerPolicy
    mesh = Mesh(args.db or "mesh.db")
    policy = PeerPolicy(trust=args.peer_trust, cap_trust=args.peer_cap)
    result = merge_peer_mesh(mesh, args.input, peer_id=args.peer_id, policy=policy)
    print(json.dumps(result, indent=2))


def cmd_benchmark(args):
    from neural_mesh import Mesh, MemoryType
    t0 = time.perf_counter()
    mesh = Mesh(args.db or ":memory:")
    if args.locomo:
        # Load LoCoMo eval
        locomo_path = args.locomo
        if not os.path.exists(locomo_path):
            print(f"LoCoMo file not found: {locomo_path}", file=sys.stderr)
            sys.exit(1)
        with open(locomo_path) as f:
            locomo_data = json.load(f)
        n_convs = len(locomo_data)
        n_questions = sum(len(c.get("qa", [])) for c in locomo_data)
        n_nodes = 0
        for conv in locomo_data:
            script = conv.get("source", "") + " " + conv.get("summary", "")
            if script.strip():
                mesh.add(script[:2000], type=MemoryType.SEMANTIC)
                n_nodes += 1
            for qa in conv.get("qa", []):
                q = qa.get("question", "")
                a = qa.get("answer", "")
                if q and a:
                    mesh.add(f"Q: {q} | A: {a}", type=MemoryType.EPISODIC)
                    n_nodes += 1
        ingest_time = time.perf_counter() - t0
        print(json.dumps({
            "benchmark": "locomo",
            "ingest_time_s": round(ingest_time, 3),
            "conversations": n_convs,
            "questions": n_questions,
            "ingested_nodes": n_nodes,
            "total_nodes": len(mesh._load()),
        }, indent=2))
    else:
        # Quick add benchmark
        for _ in range(args.count):
            mesh.add(f"benchmark node {_}: Base L2 onchain agent memory infrastructure")
        ingest_time = time.perf_counter() - t0
        nodes = len(mesh._load())
        # Resonance retrieval
        q_emb = mesh.embedder("agent memory")
        from neural_mesh.resonance import retrieve as mesh_retrieve
        t1 = time.perf_counter()
        results = mesh_retrieve(mesh._load(), q_emb, top_k=5)
        query_time = time.perf_counter() - t1
        print(json.dumps({
            "benchmark": "add+retrieve",
            "count": args.count,
            "ingest_time_s": round(ingest_time, 3),
            "query_time_s": round(query_time, 4),
            "total_nodes": nodes,
            "retrieved": len(results),
            "nodes_per_second": round(args.count / ingest_time, 1),
        }, indent=2))


def cmd_info(args):
    from neural_mesh import Mesh
    mesh = Mesh(args.db or ":memory:")
    nodes = mesh._load()
    types = {}
    for n in nodes.values():
        types[n.type.value] = types.get(n.type.value, 0) + 1
    print(json.dumps({
        "total_nodes": len(nodes),
        "type_distribution": types,
        "has_embedder": bool(mesh.embedder),
    }, indent=2))


def cmd_consolidate(args):
    from neural_mesh import MemoryLifecycle, Mesh
    lifecycle = MemoryLifecycle(Mesh(args.db or "mesh.db"))
    report = lifecycle.maintain(
        hot_ttl=args.hot_ttl,
        cold_threshold=args.cold_threshold,
        mode="sleep",
        prune_below=-1.0,
    )
    print(json.dumps(report["lanes"], indent=2))


def cmd_sleep(args):
    from neural_mesh import Mesh
    mesh = Mesh(args.db or "mesh.db")
    print(json.dumps(mesh.sleep(
        prune_below=args.prune_below,
        max_age_days=args.max_age_days,
    ), indent=2))


def cmd_pointer_put(args):
    from neural_mesh import PointerStore
    with open(args.input) as f:
        payload = f.read()
    pointer = PointerStore(args.root).put(payload, args.label)
    print(json.dumps({"pointer": pointer, "payload_chars": len(payload)}, indent=2))


def cmd_pointer_summary(args):
    from neural_mesh import PointerStore
    if not args.pointer.startswith("mesh://"):
        raise SystemExit("invalid mesh pointer")
    summary = PointerStore(args.root).summarize(args.pointer, args.max_chars)
    print(json.dumps({"pointer": args.pointer, "summary": summary}, indent=2))


def cmd_rust_info(args):
    try:
        import rust_mesh
        g = rust_mesh.Graph(100)
        g.add_edge(0, 1, 0.9)
        bfs = g.bfs(0, 2)
        bm25 = callable(getattr(rust_mesh, "bulk_bm25", None))
        print(json.dumps({
            "rust_module": "rust_mesh",
            "functions": [x for x in dir(rust_mesh) if not x.startswith("_")],
            "bm25_available": bm25,
            "bfs_test": f"{len(bfs)} nodes visited from 100-node graph",
        }, indent=2))
    except ImportError as e:
        print(json.dumps({"rust_module": "not available", "error": str(e)}))


def main():
    p = argparse.ArgumentParser(
        prog="neural-mesh",
        description="Self-organizing agentic memory mesh — v0.34.0")
    sp = p.add_subparsers(dest="cmd")

    # export
    p_export = sp.add_parser("export", help="Export mesh to .mesh file")
    p_export.add_argument("output", help="Output .mesh path")
    p_export.add_argument("--db", help="SQLite db path")

    # import
    p_import = sp.add_parser("import", help="Import .mesh file into mesh")
    p_import.add_argument("input", help="Input .mesh path")
    p_import.add_argument("--db", help="SQLite db path")
    p_import.add_argument("--no-reembed", action="store_true", help="Skip re-embedding")

    # merge
    p_merge = sp.add_parser("merge", help="Merge peer .mesh file")
    p_merge.add_argument("input", help="Peer .mesh path")
    p_merge.add_argument("--peer-id", default="peer", help="Peer agent ID")
    p_merge.add_argument("--db", default="mesh.db", help="Local mesh db")
    p_merge.add_argument("--peer-trust", type=float, default=1.0)
    p_merge.add_argument("--peer-cap", type=float, default=1.0)

    # benchmark
    p_bench = sp.add_parser("benchmark", help="Run benchmarks")
    p_bench.add_argument("--db", help="SQLite db path")
    p_bench.add_argument("--count", type=int, default=100, help="Nodes to add")
    p_bench.add_argument("--locomo", help="Path to locomo10.json for full eval")

    # info
    sp.add_parser("info", help="Mesh statistics").add_argument("--db", help="SQLite db path")

    # operational maintenance
    p_consolidate = sp.add_parser("consolidate", help="Promote durable hot memories")
    p_consolidate.add_argument("--db", default="mesh.db")
    p_consolidate.add_argument("--hot-ttl", type=float, default=86_400.0)
    p_consolidate.add_argument("--cold-threshold", type=int, default=3)

    p_sleep = sp.add_parser("sleep", help="Run replay/strengthen/prune")
    p_sleep.add_argument("--db", default="mesh.db")
    p_sleep.add_argument("--prune-below", type=float, default=0.05)
    p_sleep.add_argument("--max-age-days", type=float, default=30.0)

    p_ptr_put = sp.add_parser("pointer-put", help="Externalize a text file")
    p_ptr_put.add_argument("input")
    p_ptr_put.add_argument("--root", default=".mesh_pointers")
    p_ptr_put.add_argument("--label", default="data")

    p_ptr_summary = sp.add_parser("pointer-summary", help="Read a bounded pointer preview")
    p_ptr_summary.add_argument("pointer")
    p_ptr_summary.add_argument("--root", default=".mesh_pointers")
    p_ptr_summary.add_argument("--max-chars", type=int, default=400)

    # rust-info
    sp.add_parser("rust-info", help="Rust accelerator status")

    # version
    sp.add_parser("version", help="Print version")

    args = p.parse_args()

    if args.cmd == "export":
        cmd_export(args)
    elif args.cmd == "import":
        cmd_import(args)
    elif args.cmd == "merge":
        cmd_merge(args)
    elif args.cmd == "benchmark":
        cmd_benchmark(args)
    elif args.cmd == "info":
        cmd_info(args)
    elif args.cmd == "consolidate":
        cmd_consolidate(args)
    elif args.cmd == "sleep":
        cmd_sleep(args)
    elif args.cmd == "pointer-put":
        cmd_pointer_put(args)
    elif args.cmd == "pointer-summary":
        cmd_pointer_summary(args)
    elif args.cmd == "rust-info":
        cmd_rust_info(args)
    elif args.cmd == "version":
        print("NEURAL_MESH v0.34.0")
    else:
        p.print_help()


if __name__ == "__main__":
    main()