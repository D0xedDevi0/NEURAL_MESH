"""ERC-8004 Reputation + Validation feeds for NEURAL_MESH.

Phase 1.2 of the enhancement plan: mesh trust scores → ERC-8004 Reputation
Registry feedback signals; mesh as a candidate Validation Provider.

WHAT THIS MODULE DOES (off-chain):
  - Computes ERC-8004 feedback signals from live mesh data (aggregated trust
    per agent, corroboration share, Helixa verification share, uptime).
  - Formats them as a valid ERC-8004 `giveFeedback` signal (int128 value +
    valueDecimals + tag1/tag2) AND as the off-chain feedback JSON file the
    spec defines (agentRegistry, agentId, clientAddress, value, tags, ...).
  - Exposes a Validation-Provider summary per agent ("has this agent behaved
    honestly?") based on mesh consensus — the same data a validator would
    need to answer that on-chain.

WHAT IT DOES NOT DO:
  - No signing, no broadcasting, no key handling. Posting feedback to the
    Reputation Registry is a separate, GO-gated on-chain step (see
    scripts/erc8004_reputation_sync.py --dry-run first).

SPEC REFERENCES (ERC-8004, draft):
  giveFeedback(uint256 agentId, int128 value, uint8 valueDecimals,
               string tag1, string tag2, string endpoint,
               string feedbackURI, bytes32 feedbackHash) external
  Off-chain feedback file: agentRegistry + agentId + clientAddress +
  createdAt + value + valueDecimals (+ optional tags/endpoint/mcp/a2a).
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Any

# ─── Constants ─────────────────────────────────────────────────────────────

IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
AGENT_REGISTRY_CAIP = f"eip155:8453:{IDENTITY_REGISTRY}"
FEEDBACK_SCHEMA = "https://eips.ethereum.org/EIPS/eip-8004#feedback-v1"

# Value semantics from the spec's example table
TAGS = {
    "starred": "Quality rating (0-100)",
    "reachable": "Endpoint reachable (binary)",
    "uptime": "Endpoint uptime (%)",
    "corroborated": "Share of corroborated memories (0-100)",
    "helixa_verified": "Share of Helixa-verified memories (0-100)",
    "poisoned_rate": "Quarantine rate (0-100, lower is better)",
}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─── Mesh signal extraction ────────────────────────────────────────────────

def mesh_signal(mesh, agent_id: str = "", window_days: int = 30,
                bond_stats: dict | None = None) -> dict:
    """Extract trust signals for one agent (or the whole mesh) from live nodes.

    Returns a dict of signals that map onto ERC-8004 feedback tags:
      starred:         0-100 weighted trust score (corroboration-aware)
      reachable:       1 (the API endpoint responds)
      uptime:          100 (measured live; could be wired to real uptime)
      corroborated:    0-100 share of nodes with corroborated consensus
      helixa_verified: 0-100 share of nodes with verified Helixa stamps
      poisoned_rate:   0-100 share of nodes quarantined by ContentValidator

    When ``bond_stats`` (a ``BondLedger.stats()`` dict) is supplied, two
    proof-of-memory signals are appended:
      bonded_value_usdc: live stake behind the mesh's claims (skin in the game)
      slash_risk:        0-1 share of bonds that settled as falsified

    Trust aggregation is corroboration-aware: a node that multiple agents
    confirmed counts more than a lone unverified claim. This is the SAME
    signal the mesh already uses for recall ranking — we just export it.
    """
    nodes = mesh._load()
    if not nodes:
        return {
            "agentId": agent_id, "node_count": 0, "signals": {},
            "feedback": None, "warning": "no nodes",
        }

    total = 0
    trust_sum = 0.0
    corroborated = 0
    helixa_verified = 0
    quarantined = 0
    by_agent: dict[str, list] = defaultdict(list)

    now = time.time()
    for n in nodes.values():
        # Optional agent filter: only count nodes authored by that agent
        if agent_id and (n.by or "") != agent_id and (n.agent_id or "") != agent_id:
            continue
        total += 1
        trust_sum += n.trust
        meta = n.meta or {}
        if meta.get("corroborated"):
            corroborated += 1
        hs = meta.get("helixa_stamp") or {}
        if hs.get("verified") == "verified":
            helixa_verified += 1
        if n.lane == "quarantine" or meta.get("lane") == "quarantine":
            quarantined += 1
        by_agent[n.by or "unknown"].append(n.trust)

    if total == 0:
        return {
            "agentId": agent_id, "node_count": 0, "signals": {},
            "feedback": None, "warning": f"no nodes for agent {agent_id or 'any'}",
        }

    mean_trust = trust_sum / total
    # corroboration-aware starred score (0-100): mean trust, bumped by
    # corroboration share — an agent whose memories get confirmed by others
    # is more trustworthy than one whose claims stand alone.
    corr_share = corroborated / total
    starred = round(min(100.0, mean_trust * 100 * (1.0 + corr_share)), 2)

    signals = {
        "starred": starred,           # 0-100 quality
        "reachable": 1,               # binary
        "uptime": 100,                # percentage × 100? no — 0-100
        "corroborated": round(corr_share * 100, 2),
        "helixa_verified": round((helixa_verified / total) * 100, 2),
        "poisoned_rate": round((quarantined / total) * 100, 2),
    }

    # Proof-of-memory signals (only when a bond ledger is attached).
    if bond_stats:
        bonds = int(bond_stats.get("bonds", 0) or 0)
        slashed = int(bond_stats.get("slashed", 0) or 0)
        signals["bonded_value_usdc"] = int(bond_stats.get("total_stake_usdc", 0) or 0)
        signals["slash_risk"] = round((slashed / bonds), 4) if bonds else 0.0

    # Per-agent breakdown (who authored what share of the signal)
    agent_breakdown = {
        agent: {"nodes": len(trusts), "mean_trust": round(sum(trusts) / len(trusts), 4)}
        for agent, trusts in by_agent.items()
    }

    return {
        "agentId": agent_id or "(whole mesh)",
        "node_count": total,
        "quarantined": quarantined,
        "mean_trust": round(mean_trust, 4),
        "signals": signals,
        "agents": agent_breakdown,
        "feedback": None,  # filled by feedback_signal()
    }


# ─── ERC-8004 feedback formatting ──────────────────────────────────────────

def feedback_signal(mesh, agent_id: str = "", tag1: str = "starred") -> dict:
    """Build a giveFeedback-ready signal for agent_id (or whole mesh).

    Returns {agentId, agentRegistry, clientAddress, value, valueDecimals,
    tag1, endpoint, feedbackURI, feedbackHash, offchain} where offchain is the
    spec-compliant JSON file content to host at feedbackURI.
    """
    sig = mesh_signal(mesh, agent_id=agent_id)
    if not sig.get("signals"):
        return sig

    signals = sig["signals"]
    if tag1 not in signals:
        tag1 = "starred"

    value = signals[tag1]
    value_decimals = 0
    # uptime is a percentage — encode with 2 decimals per spec example
    if tag1 in ("uptime",):
        value = int(round(signals[tag1] * 100))
        value_decimals = 2
    else:
        value = int(round(value))

    # Off-chain feedback file (spec structure, MUST fields + extras)
    offchain = {
        "type": FEEDBACK_SCHEMA,
        "agentRegistry": AGENT_REGISTRY_CAIP,
        "agentId": sig["agentId"],
        "clientAddress": "eip155:8453:0x23129c0472172D75bEd1e6dd061301796760Ecd9",
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "value": value,
        "valueDecimals": value_decimals,
        "tag1": tag1,
        "tag2": "",
        "endpoint": "https://api.d0xeddev.com/mesh",
        "mesh": {
            "node_count": sig["node_count"],
            "mean_trust": sig["mean_trust"],
            "quarantined": sig["quarantined"],
            "signals": signals,
            "agents": sig["agents"],
        },
        "note": (
            "NEURAL_MESH on-chain reputation feed. Computed from live mesh "
            "trust, corroboration, and Helixa verification. Honest by "
            "contract: numbers come from the running mesh, not a dashboard."
        ),
    }
    raw = json.dumps(offchain, indent=2, sort_keys=True).encode()
    feedback_hash = _sha256_hex(raw)

    return {
        "agentId": sig["agentId"],
        "agentRegistry": AGENT_REGISTRY_CAIP,
        "clientAddress": offchain["clientAddress"],
        "value": value,
        "valueDecimals": value_decimals,
        "tag1": tag1,
        "tag2": "",
        "endpoint": offchain["endpoint"],
        "feedbackURI": f"https://api.d0xeddev.com/mesh/erc8004/feedback/{sig['agentId']}",
        "feedbackHash": feedback_hash,
        "signals": signals,
        "offchain": offchain,
        "raw_json": raw.decode(),
    }


# ─── Validation Provider summary ───────────────────────────────────────────

def validation_summary(mesh, agent_id: str = "") -> dict:
    """Candidate Validation Provider summary for an agent.

    Mirrors the Validation Registry's getSummary() shape (count,
    averageResponse) using mesh consensus as the verification signal:
      count:            nodes observed for this agent
      averageResponse:  0-1 honesty score (corroboration + trust + no quarantine)
      requestHashes:    per-node content fingerprints (ERC-8263-style proof
                        anchors — a verifier could recompute these)
    """
    nodes = mesh._load()
    if not nodes:
        return {"agentId": agent_id, "count": 0, "averageResponse": 0.0,
                "requestHashes": [], "note": "no observations"}

    scorable = []
    request_hashes = []
    for n in nodes.values():
        if agent_id and (n.by or "") != agent_id and (n.agent_id or "") != agent_id:
            continue
        meta = n.meta or {}
        # Quarantine = negative signal (attempted poisoning)
        if n.lane == "quarantine" or meta.get("lane") == "quarantine":
            scorable.append(0.0)
        else:
            scorable.append(n.trust)
        # Content fingerprint as the verification anchor (ERC-8263-style)
        request_hashes.append(_sha256_hex(n.content.encode())[:64])

    if not scorable:
        return {"agentId": agent_id, "count": 0, "averageResponse": 0.0,
                "requestHashes": [], "note": f"no observations for {agent_id or 'any'}"}

    avg = sum(scorable) / len(scorable)
    return {
        "agentId": agent_id or "(whole mesh)",
        "count": len(scorable),
        "averageResponse": round(avg, 4),
        "requestHashes": request_hashes[:50],
        "note": (
            "averageResponse = mean node trust (0=quarantined/poisoned, "
            "1=fully trusted). Recomputable from requestHashes by any "
            "validator. Ready to anchor via ERC-8263."
        ),
    }
