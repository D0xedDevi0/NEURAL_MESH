"""L12 — EXCHANGE: memory that travels between agents (dejavu spine port).

Every memory product treats a store as a walled garden: what one agent learns
dies with it, or is copied into another's context by hand with no provenance
and no gate. This module makes a single memory node a **portable, verifiable
artifact**:

    export_artifact()  -> the node's content + provenance + a deterministic
                          content hash covering body + provenance + identity.
    verify_artifact()  -> recompute the hash; true iff the artifact is exactly
                          as the exporting store produced it (tamper-evident).
    import_artifact()  -> verify, scan for weaponized memory, then write with
                          the ORIGIN owner tagged as provenance (so confidence
                          knows where it came from and can downgrade it).

The load-bearing result is cross-agent transfer: a buyer mesh that NEVER lived
the lesson imports the seller's node, and its confidence/known_unknowns now
reflect the foreign scar tissue — purchased and verified, not scraped.

Deterministic. Verification needs no network — the hash is self-contained.
Reuses NEURAL_MESH's existing ContentValidator (security.py) for the
content-safety scan, and the L10 `content_hash` for the digest.
"""

from __future__ import annotations

import json

from .meta import content_hash, record_provenance
from .node import MemoryType

ARTIFACT_SCHEMA = "neural-mesh/memory-node/1"


def _hashable(content: str, prov: dict, mtype: str, owner: str) -> str:
    return content_hash({
        "schema": ARTIFACT_SCHEMA, "type": mtype, "content": content,
        "provenance": prov, "owner": owner,
    })


def export_artifact(mesh, node_id: str, *, owner: str = "",
                    price_wei: int = 0) -> dict:
    """Package one live node (+ its provenance) into a portable artifact.

    The content hash covers content + provenance + identity ONLY (not price or
    timestamp), so the same node exports to an identical, verifiable artifact
    every time — two meshes can compare digests and agree on what 'this memory'
    is without trusting each other.
    """
    node = mesh._load().get(node_id)
    if node is None or node.superseded_by:
        return {"error": "absent", "node_id": node_id}
    prov = (node.meta or {}).get("provenance") or {}
    owner = owner or node.agent_id or node.by or node.provenance or "anonymous"
    body = {
        "schema": ARTIFACT_SCHEMA,
        "type": node.type.value,
        "content": node.content,
        "provenance": prov,
        "owner": owner,
        "agent_id": node.agent_id,
        "price_wei": int(price_wei),
    }
    body["content_hash"] = _hashable(node.content, prov, node.type.value, owner)
    return body


def verify_artifact(artifact: dict) -> dict:
    """Recompute the content hash and confirm the artifact is intact. Returns
    {valid: bool, reason: str} — never a bare True/False (no silent failure)."""
    try:
        if not isinstance(artifact, dict) or \
                artifact.get("schema") != ARTIFACT_SCHEMA:
            return {"valid": False, "reason": "unrecognized schema"}
        content = artifact.get("content")
        if not isinstance(content, str):
            return {"valid": False, "reason": "malformed body"}
        prov = artifact.get("provenance") or {}
        h = _hashable(content, prov, artifact.get("type", "semantic"),
                      artifact.get("owner", ""))
        if h != artifact.get("content_hash"):
            return {"valid": False, "reason": "content hash mismatch (tampered)"}
        return {"valid": True, "reason": "intact and verified"}
    except Exception as e:  # pragma: no cover
        return {"valid": False, "reason": f"verification error: {e}"}


def import_artifact(mesh, artifact: dict, *, agent_id: str = "") -> dict:
    """Verify + safety-scan a foreign node into THIS mesh.

    - verify_artifact first (a tampered/noise artifact is refused outright).
    - then scan the body with the mesh's ContentValidator and REFUSE weaponized
      memory before any write (a malicious peer can mint a well-formed artifact
      whose body carries a prompt-injection / shell idiom).
    - write the node with the ORIGIN owner tagged as provenance (so confidence
      knows where the knowledge came from and can downgrade it), and preserve
      the `hard` flag so imported scar tissue feeds the guard.
    """
    v = verify_artifact(artifact)
    if not v["valid"]:
        return {"verdict": "reject", "reason": v["reason"],
                "content_hash": artifact.get("content_hash")}

    content = artifact.get("content", "")
    owner = artifact.get("owner", "unknown")
    mtype = artifact.get("type", "semantic")
    try:
        mtype = MemoryType(mtype).value
    except ValueError:
        mtype = "semantic"

    if mesh.validator is not None:
        verdict = mesh.validator.scan(content)
        if verdict.is_malicious:
            return {"verdict": "reject",
                    "reason": f"content-safety: {[p['name'] for p in verdict.patterns]}",
                    "content_hash": artifact.get("content_hash")}

    prov = artifact.get("provenance") or {}
    node = mesh.add(
        content=content,
        type=MemoryType(mtype),
        provenance=f"imported:{owner}",
        agent_id=agent_id,
        meta={"imported": {"owner": owner,
                           "content_hash": artifact.get("content_hash"),
                           "schema": ARTIFACT_SCHEMA}},
    )
    if prov:
        record_provenance(
            mesh, node.id, source=owner, evidence=int(prov.get("evidence", 0)),
            falsifiable=bool(prov.get("falsifiable")), hard=bool(prov.get("hard")),
        )
    return {"verdict": "imported", "node_id": node.id, "owner": owner,
            "content_hash": artifact.get("content_hash"),
            "price_wei": int(artifact.get("price_wei", 0))}


def serialize(artifact: dict) -> str:
    """Canonical JSON wire form (transport-agnostic — file, bus, or network)."""
    return json.dumps(artifact, sort_keys=True, separators=(",", ":"))


def deserialize(text: str) -> dict:
    return json.loads(text)
