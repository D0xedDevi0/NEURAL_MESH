"""Reality-reconciliation gate tests — memory vs on-chain truth."""
from neural_mesh import Mesh, ReconcileGate, MATCH, MISMATCH, UNVERIFIABLE


def _gate(chain_map, fail_open=True):
    """Hermetic gate: ``chain_map`` maps (subject, addr, data) -> value."""
    def fetcher(subject, addr="", data=""):
        key = (subject, addr, data)
        if key in chain_map:
            return chain_map[key]
        # simulate a fetch failure (RPC down / no value)
        raise RuntimeError("no such chain value")
    return ReconcileGate(Mesh(db_path=":memory:"), fetcher=fetcher,
                         fail_open=fail_open)


def test_match_when_chain_agrees():
    g = _gate({("eth_balance", "0xAA", ""): 1500})
    r = g.reconcile([{"subject": "eth_balance", "address": "0xAA",
                      "operator": ">=", "value": 1000,
                      "fact": "wallet holds >= 1000 wei"}])
    assert r.allow
    assert r.verdicts[0].code == MATCH


def test_mismatch_vetoes():
    g = _gate({("eth_balance", "0xAA", ""): 5})
    r = g.reconcile([{"subject": "eth_balance", "address": "0xAA",
                      "operator": ">=", "value": 100,
                      "fact": "wallet holds >= 100 (it does not)"}])
    assert not r.allow
    assert r.verdicts[0].code == MISMATCH
    assert len(r.vetoes) == 1


def test_unverifiable_fail_open_vs_closed():
    # fail_open: RPC down -> allow with warning (no veto)
    g_open = _gate({}, fail_open=True)
    r = g_open.reconcile([{"subject": "eth_balance", "address": "0xAA",
                           "operator": ">", "value": 0}])
    assert r.allow
    assert r.verdicts[0].code == UNVERIFIABLE

    # fail_closed: RPC down -> veto (agent must never act unconfirmed)
    g_closed = _gate({}, fail_open=False)
    r2 = g_closed.reconcile([{"subject": "eth_balance", "address": "0xAA",
                              "operator": ">", "value": 0}])
    assert not r2.allow
    assert len(r2.vetoes) == 1


def test_bad_operator_is_unverifiable():
    g = _gate({})
    r = g.reconcile([{"subject": "eth_balance", "address": "0xAA",
                      "operator": "contains", "value": 1}])
    assert r.verdicts[0].code == UNVERIFIABLE


def test_multiple_claims_any_mismatch_vetoes():
    g = _gate({
        ("eth_balance", "0xA", ""): 1000,
        ("eth_balance", "0xB", ""): 1,
    })
    r = g.reconcile([
        {"subject": "eth_balance", "address": "0xA", "operator": ">", "value": 0},
        {"subject": "eth_balance", "address": "0xB", "operator": ">", "value": 50},
    ])
    assert not r.allow
    # exactly one veto (the mismatching second claim)
    assert len(r.vetoes) == 1