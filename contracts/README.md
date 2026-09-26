# PoM Escrow — Solidity contract

`PomEscrow.sol` is the on-chain half of Proof-of-Memory: agents stake USDC behind
a memory claim, and the mesh's deterministic settlement verdict moves the money
(corroborate → release, falsify → slash).

- **Source**: `contracts/PomEscrow.sol`
- **Compiled + tested in**: `/opt/data/D0XEDDEV/d0xeddev-contracts/` (Foundry,
  solc 0.8.26, OZ v5). This file is a mirror — sync from that repo when changed.
- **ABI** matches `neural_mesh/bond_escrow.py` exactly:
  - `escrowStake(bytes32 claimId, address staker, uint256 amountUsdc)`
  - `settleSlash(bytes32 claimId, address staker, address challenger, uint256 amountUsdc)`
  - `releaseStake(bytes32 claimId, address staker)`

## Test status

- **23 unit tests** (`PomEscrow.t.sol`) — green. Cover stake/slash/release, access
  control, partial slash, same-staker top-up, cross-staker hijack guard, self-release
  prohibition, bond isolation, USDC-protected recovery.
- **2 fork tests** (`PomEscrowFork.t.sol`) — green against real Base mainnet USDC
  (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`), proving `transferFrom`/`transfer`
  against the actual bridged USDC.
- Deployed bytecode **3,082 bytes** (EIP-170 limit 24,576); init **3,411 bytes** (limit 49,152).

## Deploy

Not yet deployed — GO-gated. On the Foundry mirror:

```sh
cd /opt/data/D0XEDDEV/d0xeddev-contracts
forge create contracts/PomEscrow.sol:PomEscrow \
  --rpc-url <BASE_OR_SEPOLIA_RPC> \
  --private-key "$PK" \
  --constructor-args 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 \
  --legacy --broadcast
```

The settlement relay (owner) should be the D0xedDev backend wallet after SIWA/
verdict verification — not a raw key in this repo.

## Trust model (honest)

The contract is **not** an oracle. `settleSlash`/`releaseStake` are owner-only and
move money only for a verdict `bonds.settlement_verdict` already produced off-chain
against public mesh state. Anyone can recompute that verdict, so the relay is
auditable, not a judge. Upgrades (multisig owner, timelocked slash) are future work.
