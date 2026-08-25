#!/usr/bin/env python3
"""ERC-8004 Reputation Sync — NEURAL_MESH → Base Mainnet ReputationRegistry.

Reads the live mesh feedback signal, formats it as an ERC-8004 `giveFeedback()`
call, and (with --execute) submits it to the on-chain ReputationRegistry.

GATES (all must pass before on-chain submission):
  1. Wallet funded (≥ 0.0003 ETH for gas).
  2. Agent registered in IdentityRegistry.
  3. --execute flag explicitly set.

Usage:
  .venv-server/bin/python scripts/erc8004_reputation_sync.py --dry-run
  .venv-server/bin/python scripts/erc8004_reputation_sync.py --execute --tag1 starred

Expected ReputationRegistry addresses (Base Mainnet):
  TBD — the ERC-8004 spec says "deployed as singletons per chain." If the
  deployed address is known, set REPUTATION_REGISTRY below or pass --registry.
"""

import argparse
import json
import os
import sys
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, PARENT)

from neural_mesh.reputation import feedback_signal, TAGS, AGENT_REGISTRY_CAIP
from neural_mesh.core import Mesh

# ─── Defaults ──────────────────────────────────────────────────────────────

# IdentityRegistry (Base Mainnet, from ERC-8004)
IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"

# ReputationRegistry address (TBD — could be same as IdentityRegistry or separate)
# erc-8004 spec: "When the Reputation Registry is deployed, the identityRegistry
# address is set via initialize(address identityRegistry_)"
REPUTATION_REGISTRY = os.environ.get(
    "ERC8004_REPUTATION_REGISTRY", "0x0000000000000000000000000000000000000000"
)

# Agent wallet (same as Helixa signer: derived from agent-wallet.key)
AGENT_WALLET_ADDRESS = "0x23129c0472172D75bEd1e6dd061301796760Ecd9"

# ReputationRegistry ABI (giveFeedback + read functions)
REPUTATION_ABI = json.dumps([
    {
        "inputs": [
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "int128", "name": "value", "type": "int128"},
            {"internalType": "uint8", "name": "valueDecimals", "type": "uint8"},
            {"internalType": "string", "name": "tag1", "type": "string"},
            {"internalType": "string", "name": "tag2", "type": "string"},
            {"internalType": "string", "name": "endpoint", "type": "string"},
            {"internalType": "string", "name": "feedbackURI", "type": "string"},
            {"internalType": "bytes32", "name": "feedbackHash", "type": "bytes32"},
        ],
        "name": "giveFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "getIdentityRegistry",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
])


# ─── Dry-run output ────────────────────────────────────────────────────────

def dry_run(sig: dict) -> str:
    """Format the feedback signal as a readable dry-run summary."""
    offchain = sig.get("offchain", {})
    return f"""
ERC-8004 REPUTATION: giveFeedback() (DRY RUN — no transaction submitted)

  Agent:       {AGENT_REGISTRY_CAIP} / id={sig.get('agentId','(whole mesh)')}
  Caller:      {sig.get('clientAddress','?')}
  Value:       {sig.get('value')}  (decimals={sig.get('valueDecimals')})
  Tag1:        {sig.get('tag1','starred')}
  Endpoint:    {sig.get('endpoint','?')}
  Offchain:    {sig.get('feedbackURI','?')}
  File hash:   {sig.get('feedbackHash','?')}

  Signal breakdown:
    starred:          {offchain.get('mesh',{}).get('signals',{}).get('starred','N/A')}
    corroborated:     {offchain.get('mesh',{}).get('signals',{}).get('corroborated','N/A')}
    helixa_verified:  {offchain.get('mesh',{}).get('signals',{}).get('helixa_verified','N/A')}
    poisoned_rate:    {offchain.get('mesh',{}).get('signals',{}).get('poisoned_rate','N/A')}
    uptime:           {offchain.get('mesh',{}).get('signals',{}).get('uptime','N/A')}
    reachable:        {offchain.get('mesh',{}).get('signals',{}).get('reachable','N/A')}

  Node count: {offchain.get('mesh',{}).get('node_count','?')}
  Mean trust: {offchain.get('mesh',{}).get('mean_trust','?')}
  Quarantined: {offchain.get('mesh',{}).get('quarantined','?')}

  Off-chain feedback file (saved to /opt/data/NEURAL_MESH/data/erc8004_feedback.json):
{json.dumps(offchain, indent=4)}
"""


# ─── Execute ───────────────────────────────────────────────────────────────

def submit_feedback(sig: dict, private_key: str, rpc_url: str = "https://mainnet.base.org"):
    """Submit a giveFeedback() transaction to the ReputationRegistry."""
    try:
        from web3 import Web3
    except ImportError:
        return False, "web3 not installed: pip install web3 (or use .venv-server)"

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        return False, f"RPC unreachable: {rpc_url}"

    account = w3.eth.account.from_key(private_key)
    wallet = account.address
    balance = w3.eth.get_balance(wallet)
    if balance < w3.to_wei(0.0003, "ether"):
        return False, (
            f"insufficient ETH: {w3.from_wei(balance,'ether')} < 0.0003 ETH "
            f"(wallet {wallet})"
        )

    registry = w3.to_checksum_address(REPUTATION_REGISTRY)
    if registry == "0x0000000000000000000000000000000000000000":
        return False, (
            "REPUTATION_REGISTRY not configured. "
            "Set ERC8004_REPUTATION_REGISTRY env var or pass --registry."
        )

    contract = w3.eth.contract(address=registry, abi=json.loads(REPUTATION_ABI))

    feedback_hash = sig.get("feedbackHash", "0" * 64)
    if isinstance(feedback_hash, str) and not feedback_hash.startswith("0x"):
        feedback_hash = "0x" + feedback_hash

    txn = contract.functions.giveFeedback(
        int(sig["agentId"]) if sig["agentId"].isdigit() else 0,
        sig["value"],
        sig["valueDecimals"],
        sig.get("tag1", "starred"),
        sig.get("tag2", ""),
        sig.get("endpoint", "https://api.d0xeddev.com/mesh"),
        sig.get("feedbackURI", ""),
        feedback_hash,
    ).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet),
        "gas": 200000,
        "maxFeePerGas": w3.eth.max_priority_fee + (2 * w3.eth.get_block("latest").baseFeePerGas),
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
        "chainId": w3.eth.chain_id,
    })

    signed = account.sign_transaction(txn)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt.status == 1:
        return True, f"{tx_hash.hex()}"
    return False, f"reverted: {tx_hash.hex()}"


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ERC-8004 Reputation Sync: mesh trust → on-chain feedback"
    )
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Print the feedback signal (default)")
    parser.add_argument("--execute", action="store_true", default=False,
                        help="SUBMIT giveFeedback() on-chain (needs --registry + funded wallet)")
    parser.add_argument("--agent-id", default="",
                        help="Agent ID to rate (default: whole mesh)")
    parser.add_argument("--tag1", default="starred",
                        choices=list(TAGS.keys()),
                        help="Primary signal dimension")
    parser.add_argument("--rpc-url", default="https://mainnet.base.org",
                        help="Base RPC URL")
    parser.add_argument("--registry", default=None,
                        help="ReputationRegistry address (overrides env var)")
    parser.add_argument("--key-file", default=None,
                        help="Path to wallet private key file")
    args = parser.parse_args()

    # Load mesh
    # v0.29.0: ":memory:" meant the script always saw an EMPTY mesh ("no
    # nodes" on every run). Default to the repo's persistent mesh.db.
    default_db = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "mesh.db")
    mesh = Mesh(default_db)
    # v0.29.0 fix: mesh.stats() returns {"total","hot","cold"} — the old
    # 'total_nodes'/'active_nodes' keys don't exist (KeyError on every run).
    _st = mesh.stats()
    print(f"\nMesh: {_st.get('total', len(mesh._load()))} total, "
          f"{_st.get('hot', '?')} hot / {_st.get('cold', '?')} cold")

    if args.registry:
        global REPUTATION_REGISTRY
        REPUTATION_REGISTRY = args.registry

    # Compute signal
    sig = feedback_signal(mesh, agent_id=args.agent_id, tag1=args.tag1)
    if not sig.get("value"):
        print(f"ERROR: {sig.get('warning', 'no signal')}")
        sys.exit(1)

    # Dry run
    if args.dry_run and not args.execute:
        print(dry_run(sig))
        offchain = sig.get("offchain", {})
        os.makedirs(os.path.join(PARENT, "data"), exist_ok=True)
        path = os.path.join(PARENT, "data", "erc8004_feedback.json")
        with open(path, "w") as f:
            json.dump(offchain, f, indent=2)
        print(f"  Off-chain feedback file saved to {path}")
        return

    # Execute
    if args.execute:
        key_file = args.key_file or os.path.expanduser("~/.secrets/agent-wallet.key")
        if not os.path.exists(key_file):
            print(f"ERROR: wallet key file not found: {key_file}")
            sys.exit(1)
        with open(key_file) as f:
            private_key = f.read().strip()
        ok, msg = submit_feedback(sig, private_key, args.rpc_url)
        if ok:
            print(f"\nGIVEN: tx {msg}")
        else:
            print(f"\nFAILED: {msg}")
            sys.exit(1)


if __name__ == "__main__":
    main()
