"""Self-editing memory blocks (neural_mesh.memory_blocks)."""
import tempfile
from pathlib import Path

import pytest

from neural_mesh import Mesh
from neural_mesh.memory_blocks import BlockLimitExceeded, MemoryBlocks


def _mb() -> MemoryBlocks:
    d = tempfile.mkdtemp()
    return MemoryBlocks(Mesh(db_path=str(Path(d) / "m.db")))


def test_create_and_get_roundtrip():
    mb = _mb()
    b = mb.create("persona", "I am Devio.", description="who I am", limit=500)
    assert b.label == "persona"
    assert b.value == "I am Devio."
    assert b.chars_current == 11
    assert b.chars_remaining == 489
    assert mb.value("persona") == "I am Devio."
    assert mb.get("nope") is None


def test_duplicate_create_is_refused():
    mb = _mb()
    mb.create("human", "Cody")
    with pytest.raises(ValueError):
        mb.create("human", "someone else")


def test_append_within_budget_succeeds():
    mb = _mb()
    mb.create("notes", "a", limit=10)
    b = mb.append("notes", "bc")
    assert b.value == "abc"
    assert b.version == 2


def test_append_over_budget_is_refused_and_atomic():
    """The load-bearing property: the budget is enforced at write time, and a
    refused write must leave the block byte-identical (no partial append)."""
    mb = _mb()
    mb.create("persona", "12345", limit=6)
    with pytest.raises(BlockLimitExceeded):
        mb.append("persona", "6789")  # would be 9 > 6
    b = mb.get("persona")
    assert b.value == "12345", "refused append mutated the block"
    assert b.version == 1, "refused append created a version"


def test_exact_limit_boundary_is_allowed():
    mb = _mb()
    mb.create("b", "12345", limit=6)
    assert mb.append("b", "6").chars_current == 6  # exactly at the limit, allowed
    with pytest.raises(BlockLimitExceeded):
        mb.append("b", "7")


def test_read_only_block_rejects_mutation():
    mb = _mb()
    mb.create("policies", "no secrets in commits", read_only=True)
    for op in (lambda: mb.append("policies", " x"),
               lambda: mb.set("policies", "new"),
               lambda: mb.replace("policies", "no", "yes")):
        with pytest.raises(PermissionError):
            op()
    assert mb.value("policies") == "no secrets in commits"


def test_replace_requires_match():
    mb = _mb()
    mb.create("human", "Cody lives in NY")
    assert mb.replace("human", "NY", "upstate NY").value == "Cody lives in upstate NY"
    with pytest.raises(ValueError):
        mb.replace("human", "nonexistent", "x")


def test_history_accumulates_versions_and_revert_restores():
    mb = _mb()
    mb.create("persona", "v1")
    mb.append("persona", " -> v2")
    mb.append("persona", " -> v3")
    hist = mb.history("persona")
    assert [b.version for b in hist] == [1, 2, 3]
    assert hist[0].value == "v1"
    # revert writes a NEW version (history is never rewritten)
    b = mb.revert("persona", 1)
    assert b.value == "v1"
    assert b.version == 4
    assert [x.version for x in mb.history("persona")] == [1, 2, 3, 4]


def test_revert_unknown_version_raises():
    mb = _mb()
    mb.create("persona", "x")
    with pytest.raises(KeyError):
        mb.revert("persona", 99)


def test_blocks_persist_across_instances():
    d = tempfile.mkdtemp()
    db = str(Path(d) / "m.db")
    MemoryBlocks(Mesh(db_path=db)).create("persona", "persisted", limit=100)
    assert MemoryBlocks(Mesh(db_path=db)).value("persona") == "persisted"


def test_list_and_render_shape():
    mb = _mb()
    mb.create("persona", "I am Devio", description="who I am", limit=100)
    mb.create("human", "Cody", limit=100, read_only=True)
    assert [b.label for b in mb.list()] == ["human", "persona"]
    out = mb.render()
    assert out.startswith("<memory_blocks>") and out.endswith("</memory_blocks>")
    assert "<persona>" in out and "chars_limit=100" in out
    assert "read_only=true" in out
    assert "who I am" in out


def test_mutation_on_missing_block_raises_keyerror():
    mb = _mb()
    with pytest.raises(KeyError):
        mb.append("ghost", "x")
    with pytest.raises(KeyError):
        mb.set("ghost", "x")