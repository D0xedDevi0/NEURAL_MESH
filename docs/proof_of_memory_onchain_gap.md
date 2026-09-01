# Proof-of-Memory — On-chain Escrow: Funding & Deploy Gap

## What is live NOW (dry-run, zero gas)

`neural_mesh/bonds.py` (BondLedger) + `neural_mesh/bond_escrow.py` (BondEscrow)
settle every PoM verdict **deterministically** in micro-USDC integers:

- `stake_claim` / `corroborate_claim` / `challenge_claim` / `settle_claim` /
  `release_claim` — the full bond lifecycle.
- Settlement reuses the mesh's OWN truth machinery (versioning / consensus /
  quarantine / staleness) — no central oracle.
- `build_escrow_calldata()` emits the real ABI-encoded calldata for
  `escrowStake` / `settleSlash` / `releaseStake` on Base USDC, dry-run only.

This is not simulated "vibes": the economics are real integers, the verdicts
are real, the calldata is real. The only thing not yet done is *moving actual
USDC on Base*.

## What blocks going fully on-chain

| Blocker | Status | What it needs |
| --- | --- | --- |
| `POM_ESCROW_CONTRACT` | NOT deployed | A minimal Solidity escrow (`escrowStake`/`settleSlash`/`releaseStake` over ERC-20 USDC, settling against a verdict hash the mesh submits). Deploy + verify on Base Mainnet. |
| Funded signer | NOT provisioned | A key-held broadcaster (separate flow, never in this module). Needs a wallet with USDC + Base ETH for gas. |
| Live USDC on participants | NOT funded | Stakers/challengers need USDC to stake/counter-stake. |

## Why this is the correct order

1. The dry-run ledger already proves the economic gate: **lying is
   net-negative with a bond, free without one** (see
   `bench/bond_economics.py`, `docs/assets/bond_economics.svg`).
2. On-chain escrow is the *last mile* — it adds gas + custody risk but zero
   new truth logic (the settlement function is identical). Shipping it before
   the dry-run proof is a distraction; shipping it without a funded signer is
   fake.

## Honest contract

This repository does **not** claim live on-chain USDC settlement. It claims,
and proves, that the settlement function is deterministic, the economics gate
reproducibly penalizes lying, and the exact calldata for on-chain settlement is
emitted and verified dry-run. Going live requires a deploy + a funded signer —
both explicitly GO-gated, never faked.

Run the honest status any time:

```bash
PYTHONPATH=. python3 -c "from neural_mesh.bond_escrow import escrow_status; print(escrow_status())"
```
