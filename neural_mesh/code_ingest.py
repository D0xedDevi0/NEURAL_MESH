"""Code → typed-graph ingestion for NEURAL_MESH.

Cognee-style pattern, MESH-native: parse a repository's source into SYMBOL nodes
(module / class / function / method) plus typed EDGES (contains / imports /
calls / inherits), so an agent can *retrieve* over the shape and dependencies of
a codebase the same way it retrieves over its own memories.

Why this and not a graph DB: Cognee builds a code knowledge graph but wants a
vector store + LLM extraction pipeline. NEURAL_MESH already *is* a typed graph
with hybrid retrieval, so the whole thing is stdlib `ast` + the mesh's own
`_save`/links primitives. No new dependency, no separate store.

Deterministic ids make re-ingest idempotent (REPLACE on the same primary key),
so running it twice on the same tree does not duplicate nodes.

Usage:
    PYTHONPATH=. python3 -m neural_mesh.code_ingest /path/to/repo --db code.db
    PYTHONPATH=. python3 -m neural_mesh.code_ingest /path/to/repo --json
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .node import MemoryNode, MemoryType

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", ".tox", "site-packages",
}


@dataclass
class Symbol:
    id: str
    kind: str          # module|class|function|method
    qualname: str
    path: str          # repo-relative
    lineno: int
    content: str
    doc: str = ""
    parent: str = ""          # owner node id (module id, or class id for methods)
    bases: list = field(default_factory=list)
    calls: list = field(default_factory=list)     # bare names
    imports: list = field(default_factory=list)   # dotted module names


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def module_name(rel: str) -> str:
    """'neural_mesh/core.py' -> 'neural_mesh.core'; '__init__.py' folds up."""
    p = rel[:-3] if rel.endswith(".py") else rel
    parts = p.split(os.sep)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _sig(node: ast.AST) -> str:
    try:
        return ast.unparse(node.args)  # py3.9+
    except Exception:
        return "()"


def parse_file(path: Path, root: Path, ns: str = "") -> list[Symbol]:
    """Parse one Python file into module + class + function/method symbols.

    `ns` optionally namespaces the node ids + stored path so several repos can
    share one mesh without id collisions (e.g. ns='NEURAL_MESH').
    """
    rel = _rel(path, root)
    key = f"{ns}/{rel}" if ns else rel
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    mod = module_name(rel)
    n_lines = src.count("\n") + 1
    n_defs = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 for n in ast.walk(tree))

    syms: list[Symbol] = []
    mod_id = f"code:module:{key}"
    mod_calls: list[str] = []
    mod_imports: list[str] = []
    n_classes = n_funcs = 0

    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if isinstance(n, ast.ImportFrom):
                if n.module and n.level == 0:
                    mod_imports.append(n.module)
            else:
                for a in n.names:
                    mod_imports.append(a.name)

    # pre-count for the module summary
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            n_classes += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n_funcs += 1

    syms.append(Symbol(
        id=mod_id, kind="module", qualname=mod, path=key, lineno=1,
        content=(f"module {mod} ({key}) — {n_lines} lines; "
                 f"{n_classes} classes, {n_funcs} functions"),
        doc=(ast.get_docstring(tree) or "").split("\n")[0][:200],
        imports=mod_imports,
    ))

    def walk_body(body, owner_qual: str, owner_id: str, in_class: bool = False):
        for n in body:
            if isinstance(n, ast.ClassDef):
                cq = f"{owner_qual}.{n.name}" if owner_qual else n.name
                cid = f"code:sym:{key}:{cq}"
                doc = (ast.get_docstring(n) or "").split("\n")[0][:200]
                bases = [b.id if isinstance(b, ast.Name) else
                         (b.attr if isinstance(b, ast.Attribute) else "")
                         for b in n.bases]
                syms.append(Symbol(
                    id=cid, kind="class", qualname=cq, path=key, lineno=n.lineno,
                    content=f"class {cq} in {key}:{n.lineno}"
                            + (f"({', '.join(b for b in bases if b)})" if any(bases) else "")
                            + (f" — {doc}" if doc else ""),
                    doc=doc, parent=owner_id, bases=[b for b in bases if b],
                ))
                walk_body(n.body, cq, cid, in_class=True)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                fq = f"{owner_qual}.{n.name}" if owner_qual else n.name
                fid = f"code:sym:{key}:{fq}"
                doc = (ast.get_docstring(n) or "").split("\n")[0][:200]
                calls = [
                    (c.func.id if isinstance(c.func, ast.Name)
                     else c.func.attr if isinstance(c.func, ast.Attribute) else "")
                    for c in ast.walk(n) if isinstance(c, ast.Call)
                ]
                calls = [c for c in calls if c and c != n.name]
                sig = f"{n.name}({_sig(n)})"
                syms.append(Symbol(
                    id=fid, kind=kind, qualname=fq, path=key, lineno=n.lineno,
                    content=f"{kind} {fq} in {key}:{n.lineno} — signature {sig}"
                            + (f" — {doc}" if doc else ""),
                    doc=doc, parent=owner_id, calls=calls,
                ))
                if not in_class:
                    mod_calls.extend(calls)

    walk_body(tree.body, mod, mod_id)
    syms[0].calls = mod_calls          # module-level call seeds
    return syms


def scan_repo(root: Path, max_files: int = 0, include_tests: bool = True,
              ns: str = "") -> list[Symbol]:
    syms: list[Symbol] = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            if not include_tests and (fn.startswith("test_") or fn.endswith("_test.py")):
                continue
            syms.extend(parse_file(Path(dirpath) / fn, root, ns))
            files += 1
            if max_files and files >= max_files:
                return syms
    return syms


class CodeGraph:
    """Ingest a repo's symbols + edges into a Mesh. Idempotent by node id."""

    def __init__(self, mesh, root: str | Path, include_tests: bool = True,
                 link_threshold: float = 0.0, namespace: "str | None" = None):
        self.mesh = mesh
        self.root = Path(root)
        self.include_tests = include_tests
        self.link_threshold = link_threshold
        # None -> derive from the directory name (multi-repo safety);
        # ""   -> no namespace (single-repo / tests).
        self.namespace = self.root.name if namespace is None else namespace
        self.stats = {"nodes": 0, "edges": 0, "by_kind": {}, "by_rel": {}}

    def _intern(self, sym: Symbol) -> str:
        n = MemoryNode(
            id=sym.id,
            type=MemoryType.PROCEDURAL,
            content=sym.content,
            embedding=self.mesh.embedder(sym.content),
            lane="cold",                 # code is long-term reference, not hot ctx
            provenance="code",
            by="code-ingest",
            trust=0.9,
            meta={
                "code": True, "kind": sym.kind, "qualname": sym.qualname,
                "path": sym.path, "lineno": sym.lineno,
                "doc": sym.doc, "bases": sym.bases,
            },
        )
        self.mesh._save(n)
        self.stats["nodes"] += 1
        self.stats["by_kind"][sym.kind] = self.stats["by_kind"].get(sym.kind, 0) + 1
        return sym.id

    def _edge(self, src_id: str, rel: str, dst_id: str, weight: float = 1.0):
        if src_id == dst_id:
            return
        nodes = self.mesh._load()
        src = nodes.get(src_id)
        if src is None:
            return
        src.links[f"{rel}::{dst_id}"] = round(weight, 3)
        self.mesh._save(src)
        self.stats["edges"] += 1
        self.stats["by_rel"][rel] = self.stats["by_rel"].get(rel, 0) + 1

    def ingest(self) -> dict:
        syms = scan_repo(self.root, include_tests=self.include_tests,
                         ns=self.namespace)
        by_id = {s.id: s for s in syms}

        # module dotted-name -> module node id (for import resolution)
        mod_index = {module_name(s.path): s.id for s in syms if s.kind == "module"}
        # bare symbol name -> [node ids] (for call resolution)
        name_index: dict[str, list[str]] = {}
        for s in syms:
            if s.kind in ("function", "method", "class"):
                name_index.setdefault(s.qualname.split(".")[-1], []).append(s.id)

        for s in syms:
            self._intern(s)

        for s in syms:
            # contains: owner (module or class) -> child symbol
            if s.parent:
                self._edge(s.parent, "contains", s.id)
            if s.kind == "module":
                # imports: module -> module (local only, resolved)
                for mod in set(s.imports):
                    target = mod_index.get(mod)
                    if not target:
                        # from a.b.c import x  -> also try parent packages
                        parts = mod.split(".")
                        for k in range(len(parts), 0, -1):
                            target = mod_index.get(".".join(parts[:k]))
                            if target:
                                break
                    if target:
                        self._edge(s.id, "imports", target)
            elif s.kind == "class":
                for b in s.bases:
                    for cid in name_index.get(b, []):
                        if by_id.get(cid, None) and by_id[cid].kind == "class":
                            self._edge(s.id, "inherits", cid)
            if s.kind in ("function", "method", "module"):
                own_mod = s.path
                for c in set(s.calls):
                    for cid in name_index.get(c, []):
                        tgt = by_id.get(cid)
                        if not tgt:
                            continue
                        # prefer same-module call targets
                        w = 1.0 if tgt.path == own_mod else 0.5
                        self._edge(s.id, "calls", cid, w)

        self.mesh._invalidate_cache()
        return self.stats


def main(argv=None) -> int:
    import argparse
    from .core import Mesh

    ap = argparse.ArgumentParser(description="Ingest a codebase as a typed symbol graph into NEURAL_MESH.")
    ap.add_argument("repo", help="path to the repository root")
    ap.add_argument("--db", default="code-graph.db", help="mesh sqlite path")
    ap.add_argument("--no-tests", action="store_true", help="skip test_*.py")
    ap.add_argument("--ns", default=None,
                    help="namespace for node ids (default: repo dir name)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    mesh = Mesh(db_path=args.db)
    g = CodeGraph(mesh, args.repo, include_tests=not args.no_tests,
                  namespace=args.ns)
    stats = g.ingest()

    if args.json:
        print(json.dumps(stats, indent=2))
        return 0
    print(f"🟦 code graph → {args.db}")
    print(f"  nodes: {stats['nodes']}  edges: {stats['edges']}")
    for k, v in sorted(stats["by_kind"].items()):
        print(f"    {k}: {v}")
    for r, v in sorted(stats["by_rel"].items()):
        print(f"    {r} edges: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())