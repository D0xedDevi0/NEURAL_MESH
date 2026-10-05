"""Code → typed-graph ingestion tests (neural_mesh.code_ingest).

Contract: parsing a Python tree yields module/class/function/method nodes with
deterministic ids, typed edges (contains/imports/calls/inherits), idempotent
re-ingest, and the graph is retrievable through normal mesh recall.
"""
import tempfile
from pathlib import Path

from neural_mesh import Mesh
from neural_mesh.code_ingest import CodeGraph, module_name, parse_file, scan_repo

A_PY = '''
"""Module A."""
import json
from pkg import c


class Base:
    """A base class."""
    def ping(self):
        return helper(1)


def helper(x):
    """Do the help."""
    return json.dumps(x) + c.TAG
'''

B_PY = '''
from pkg.a import Base, helper


class Sub(Base):
    """A subclass."""
    def run(self):
        return helper(2)
'''

C_PY = 'TAG = "c"\n'


def _repo(tmp: Path):
    pkg = tmp / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(A_PY)
    (pkg / "b.py").write_text(B_PY)
    (pkg / "c.py").write_text(C_PY)
    return tmp


def test_module_name_maps_init_up():
    assert module_name("pkg/a.py") == "pkg.a"
    assert module_name("pkg/__init__.py") == "pkg"


def test_parse_file_extracts_kinds():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        syms = parse_file(root / "pkg" / "a.py", root)
        kinds = {s.qualname: s.kind for s in syms}
        assert kinds["pkg.a"] == "module"
        assert kinds["pkg.a.Base"] == "class"
        assert kinds["pkg.a.Base.ping"] == "method"
        assert kinds["pkg.a.helper"] == "function"


def test_ingest_builds_nodes_and_typed_edges():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        mesh = Mesh(db_path=str(Path(d) / "m.db"))
        stats = CodeGraph(mesh, root, namespace="").ingest()
        assert stats["nodes"] >= 5
        assert stats["by_rel"].get("imports", 0) >= 1
        assert stats["by_rel"].get("inherits", 0) >= 1
        assert stats["by_rel"].get("calls", 0) >= 1

        nodes = mesh._load()
        sub = nodes["code:sym:pkg/b.py:pkg.b.Sub"]
        # inherits edge: Sub -> Base, encoded as "inherits::<id>"
        assert any(k.startswith("inherits::code:sym:pkg/a.py:pkg.a.Base")
                   for k in sub.links)

        b_mod = nodes["code:module:pkg/b.py"]
        assert any(k.startswith("imports::code:module:pkg/a.py") for k in b_mod.links)


def test_reingest_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        db = str(Path(d) / "m.db")
        mesh = Mesh(db_path=db)
        CodeGraph(mesh, root, namespace="").ingest()
        n1 = len(mesh._load())
        mesh2 = Mesh(db_path=db)
        CodeGraph(mesh2, root, namespace="").ingest()
        n2 = len(mesh2._load())
        assert n1 == n2                      # REPLACE on deterministic id


def test_graph_is_retrievable():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        mesh = Mesh(db_path=str(Path(d) / "m.db"))
        CodeGraph(mesh, root, namespace="").ingest()
        hits = mesh.recall("Sub class inherits Base", top_k=5)
        assert hits
        assert any("Sub" in n.content for n in hits)


def test_scan_repo_skips_venv_and_pycache():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        (root / ".venv").mkdir()
        (root / ".venv" / "junk.py").write_text("x = 1\n")
        (root / "pkg" / "__pycache__").mkdir()
        (root / "pkg" / "__pycache__" / "j.py").write_text("y = 2\n")
        syms = scan_repo(root)
        assert all(".venv" not in s.path and "__pycache__" not in s.path for s in syms)


def test_namespaces_keep_multiple_repos_distinct():
    """Two repos with identical relative paths must not collide in one mesh."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        r1, r2 = base / "repoA", base / "repoB"
        _repo(r1)
        _repo(r2)
        mesh = Mesh(db_path=str(base / "m.db"))
        CodeGraph(mesh, r1, namespace="repoA").ingest()
        CodeGraph(mesh, r2, namespace="repoB").ingest()
        nodes = mesh._load()
        assert "code:module:repoA/pkg/a.py" in nodes
        assert "code:module:repoB/pkg/a.py" in nodes
        assert nodes["code:module:repoA/pkg/a.py"].meta["path"] == "repoA/pkg/a.py"


def test_default_namespace_is_directory_name():
    with tempfile.TemporaryDirectory() as d:
        root = _repo(Path(d))
        mesh = Mesh(db_path=str(Path(d) / "m.db"))
        g = CodeGraph(mesh, root)
        assert g.namespace == root.name
        g.ingest()
        assert all(k.startswith(f"code:module:{root.name}/") or ":" in k
                   for k in mesh._load())