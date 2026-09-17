"""Bi-temporal recall tests — the "what did we believe WHEN" contract."""
import time

from neural_mesh import Mesh, snapshot, recall_asof, history, resolve_at
from neural_mesh.temporal import valid_at


def _m():
    return Mesh(db_path=":memory:")


def test_valid_at_open_interval():
    n = _m().add("lead is Alice", valid_from=100.0)
    assert valid_at(n, 100.0) and valid_at(n, 9999.0)
    assert not valid_at(n, 99.0)


def test_supersede_stamps_valid_to_and_filters_snapshot():
    m = _m()
    a = m.add("lead is Alice")
    time.sleep(0.02)                      # real gap so the window is measurable
    b = m.add("lead is Bob", supersedes=a.id)
    a = m._load()[a.id]  # _supersede stamps a fresh copy; reload to observe it
    # The old fact ended; the new fact is open-ended.
    assert a.valid_to > 0
    assert b.valid_to == 0

    mid = (a.valid_from + a.valid_to) / 2   # while Alice was still current
    before = snapshot(m, as_of=mid)
    after = snapshot(m, as_of=a.valid_to + 0.5)
    assert any(n.content == "lead is Alice" for n in before)
    assert any(n.content == "lead is Bob" for n in after)
    assert not any(n.content == "lead is Alice" for n in after)


def test_recall_asof_returns_only_then_true_facts():
    m = _m()
    alice = m.add("ceo is Alice")
    t_mid = alice.valid_to or time.time()  # during Alice's reign
    m.add("ceo is Bob", supersedes=alice.id)

    hits_before = recall_asof(m, "who runs the company", as_of=t_mid, top_k=3)
    hits_after = recall_asof(m, "who runs the company",
                             as_of=time.time() + 5, top_k=3)
    assert any("Alice" in n.content for n in hits_before)
    assert any("Bob" in n.content for n in hits_after)
    assert not any("Alice" in n.content for n in hits_after)


def test_history_reconstructs_supersede_chain():
    m = _m()
    v1 = m.add("price is 10")
    v2 = m.add("price is 20", supersedes=v1.id)
    m.add("price is 30", supersedes=v2.id)
    chain = history(m, v2.id)
    contents = [c["content"] for c in chain]
    assert any("10" in c for c in contents)
    assert any("20" in c for c in contents)
    assert any("30" in c for c in contents)
    for c in chain:
        assert "valid_from" in c and "valid_to" in c


def test_resolve_at_pins_historical_version():
    m = _m()
    v1 = m.add("owner is alice")
    t1 = v1.valid_to or time.time()   # while v1 current
    v2 = m.add("owner is bob", supersedes=v1.id)
    assert resolve_at(m, v1.id, t1).content == "owner is alice"
    assert resolve_at(m, v2.id, time.time() + 1).content == "owner is bob"


def test_explicit_valid_from_into_past():
    n = Mesh(db_path=":memory:").add("jan leader was Carol", valid_from=1700000000.0)
    assert n.valid_from == 1700000000.0
    assert valid_at(n, 1750000000.0)
    assert not valid_at(n, 1650000000.0)