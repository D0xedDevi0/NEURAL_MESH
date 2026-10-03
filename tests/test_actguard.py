"""ExecutionGuard tests — the load-bearing veto between memory and action."""
from neural_mesh import Mesh, ExecutionGuard, MATCH, MISMATCH, UNVERIFIABLE


def _guard(chain_map, fail_open=True, bond_ledger=None):
    """Hermetic guard: ``chain_map`` maps (subject, addr, data) -> value."""
    def fetcher(subject, addr="", data=""):
        key = (subject, addr, data)
        if key in chain_map:
            return chain_map[key]
        raise RuntimeError("no such chain value")

    return ExecutionGuard(Mesh(db_path=":memory:"), fetcher=fetcher,
                          fail_open=fail_open, bond_ledger=bond_ledger)


def test_authorize_swap_match_allows():
    chain = {
        ("erc20_balance", "0xTOKEN", "0xWALLET"): 1000,
        ("eth_balance", "0xWALLET", ""): 5000,
        ("contract_code", "0xPOOL", ""): 1,
    }
    g = _guard(chain)
    v = g.authorize_swap(token_in="0xTOKEN", token_out="0xOUT", pool="0xPOOL",
                         size_wei=500, wallet="0xWALLET", gas_reserve_wei=1000)
    assert v.allow
    assert bool(v)
    # exactly three canonical claims were derived
    assert len(v.verdicts) == 3
    assert all(x.code == MATCH for x in v.verdicts)


def test_authorize_swap_balance_mismatch_vetoes():
    """THE money shot: mesh believes it holds >= 500, chain says 100 -> VETO."""
    chain = {
        ("erc20_balance", "0xTOKEN", "0xWALLET"): 100,
        ("eth_balance", "0xWALLET", ""): 5000,
        ("contract_code", "0xPOOL", ""): 1,
    }
    g = _guard(chain)
    v = g.authorize_swap(token_in="0xTOKEN", token_out="0xOUT", pool="0xPOOL",
                         size_wei=500, wallet="0xWALLET", gas_reserve_wei=1000)
    assert not v.allow
    assert not bool(v)
    assert len(v.vetoes) == 1


def test_authorize_unverifiable_fail_closed_vetoes():
    chain = {}
    g = _guard(chain, fail_open=False)
    v = g.authorize_swap(token_in="0xTOKEN", token_out="0xOUT", pool="0xPOOL",
                         size_wei=500, wallet="0xWALLET")
    assert not v.allow
    assert all(x.code == UNVERIFIABLE for x in v.verdicts)


def test_bonded_claim_mismatch_emits_slash_flag():
    chain = {("eth_balance", "0xAA", ""): 5}
    g = _guard(chain, bond_ledger=object())
    v = g.authorize(
        action={"kind": "payment", "to": "0xBB", "amount": 100},
        claims=[{"subject": "eth_balance", "address": "0xAA", "operator": ">=",
                 "value": 100, "bond_id": "b-1",
                 "fact": "bonded: wallet holds >= 100"}],
    )
    assert not v.allow
    assert len(v.slash_flags) == 1
    assert v.slash_flags[0]["bond_id"] == "b-1"


def test_unbonded_mismatch_emits_no_slash_flag():
    chain = {("eth_balance", "0xAA", ""): 5}
    g = _guard(chain, bond_ledger=object())
    v = g.authorize(
        action={"kind": "payment", "to": "0xBB", "amount": 100},
        claims=[{"subject": "eth_balance", "address": "0xAA", "operator": ">=",
                 "value": 100, "fact": "unbonded belief"}],
    )
    assert not v.allow
    assert len(v.slash_flags) == 0


def test_extra_claims_are_checked():
    chain = {
        ("erc20_balance", "0xTOKEN", "0xWALLET"): 1000,
        ("eth_balance", "0xWALLET", ""): 5000,
        ("contract_code", "0xPOOL", ""): 1,
        ("eth_balance", "0xOTHER", ""): 3,
    }
    g = _guard(chain)
    v = g.authorize_swap(
        token_in="0xTOKEN", token_out="0xOUT", pool="0xPOOL",
        size_wei=500, wallet="0xWALLET",
        extra_claims=[{"subject": "eth_balance", "address": "0xOTHER",
                       "operator": ">=", "value": 100,
                       "fact": "sponsor covers gas"}],
    )
    assert not v.allow  # the extra claim mismatches -> veto
    assert len(v.vetoes) == 1