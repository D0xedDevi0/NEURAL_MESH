"""Federated DREAM — the self-healing memory commons.

WHY
---
v0.30 gave agents a way to *pay* for each other's memory (demand). v0.31 adds
the *supply* path: an agent runs its DREAM consolidation cycle, mints insight
nodes, and publishes them to a shared commons. But nothing enters the live mesh
without clearing a gate, so the commons stays honest:

  1. REPUTATION GATE — refuse low-rep / unknown contributors (you don't ingest
     wisdom from an agent you can't vet).
  2. POISON SCAN — every contribution passes the ContentValidator (OWASP ASI06
     memory-poisoning defense); malicious insight → quarantine lane, never live.
  3. CORROBORATION — an insight matching an existing live node gets a trust bump
     (trust = 1-(1-ta)(1-tb)); corroborated wisdom compounds.
  4. WRITEBACK — accepted insight lands with provenance preserved
     (provenance="federated-dream", by=<contributor>, source mesh in meta).

This is "natural selection onchain" made complete: honest corroborated wisdom
spreads and compounds; garbage and poison get quarantined or refused.

Pure stdlib. Composes the already-shipped ContentValidator (security.py), the
reputation-gate pattern from federation.py, and DREAM insight minting.

USAGE
-----
    from neural_mesh.federated_dream import FederatedDream

    fd = FederatedDream(mesh, min_rep=50.0)
    report = fd.receive(
        contributions=[
            {"content": "Base scales via optimistic rollups.",
             "by": "peer-b", "agent_id": "peer-b", "trust": 0.9, "rep": 88.0},
        ]
    )
    # report["verdicts"] = per-insight accepted / corroborated / quarantined / refused
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .security import ContentValidator
from .sharing import _content_hash


@dataclass
class InsightVerdict:
    """Outcome for one contributed insight."""
    content: str
    verdict: str            # "accepted" | "corroborated" | "quarantined" | "refused"
    reason: str = ""
    trust: float | None = None     # post-gate trust (accepted/corroborated)
    node_id: str | None = None     # id of the landed node (accepted/quarantined)
    corroborated: bool = False
    by: str = ""


class FederatedDream:
    """Gate + writeback DREAM insight contributions into a local mesh.

    *mesh* — the receiving Mesh.
    *min_rep* — ERC-8004 reputation floor (0-100); contributions below it refused.
    *cap_trust* — ceiling on any contributed insight's trust.
    *validator* — ContentValidator (default: fresh instance). Pass False to
                bypass the poison scan (testing only).
    *quarantine_policy* — "strict" (malicious+suspicious→quarantine),
                "malicious-only" (default), or "off".
    """

    def __init__(
        self,
        mesh,
        *,
        min_rep: float = 50.0,
        cap_trust: float = 0.9,
        validator=None,
        quarantine_policy: str = "malicious-only",
    ):
        self._mesh = mesh
        self.min_rep = min_rep
        self.cap_trust = cap_trust
        self._validator = ContentValidator() if validator is None else validator
        self.quarantine_policy = quarantine_policy

    # ── Contribute ────────────────────────────────────────────────────────
    @staticmethod
    def package_insights(nodes, source_url: str = "") -> list[dict]:
        """Package a list of MemoryNode (or dicts) into portable contributions."""
        out = []
        for n in nodes:
            if hasattr(n, "content"):
                out.append({
                    "content": n.content,
                    "by": getattr(n, "by", "") or getattr(n, "agent_id", "") or "self",
                    "agent_id": getattr(n, "agent_id", ""),
                    "trust": getattr(n, "trust", 0.5),
                    "provenance": getattr(n, "provenance", ""),
                    "source_url": source_url,
                })
            elif isinstance(n, dict):
                out.append({
                    "content": n.get("content", ""),
                    "by": n.get("by", "") or n.get("agent_id", "") or "self",
                    "agent_id": n.get("agent_id", ""),
                    "trust": n.get("trust", 0.5),
                    "provenance": n.get("provenance", ""),
                    "source_url": source_url or n.get("source_url", ""),
                })
        return out

    # ── Gate ──────────────────────────────────────────────────────────────
    def _scan(self, content: str) -> tuple[str, str]:
        """Run the poison scan. Returns (verdict_level, reason)."""
        v = self._validator.scan(content)
        if self.quarantine_policy == "off":
            return v.level, "scanned (policy off)"
        if v.is_malicious:
            return "quarantined", "malicious: " + (v.patterns[0]["name"] if v.patterns else "poison")
        if v.is_suspicious and self.quarantine_policy == "strict":
            return "quarantined", "suspicious (strict): " + (v.patterns[0]["name"] if v.patterns else "suspicious")
        return v.level, "safe"

    def _corroborate_against_local(self, content: str) -> bool:
        """True if an existing live node has the same content hash."""
        key = _content_hash(content)
        for n in self._mesh._load().values():
            if n.superseded_by or n.lane == "quarantine":
                continue
            if _content_hash(n.content) == key:
                return True
        return False

    # ── Receive ───────────────────────────────────────────────────────────
    def receive(self, contributions: list[dict],
                *,
                default_rep: float | None = None,
                writeback: bool = True) -> dict:
        """Gate + optionally writeback a list of contributed insights.

        Each contribution may carry ``rep`` (its contributor's reputation); else
        ``default_rep`` is used (None = unknown → refused).

        Returns {"verdicts": [...], "accepted": N, "corroborated": N,
                 "quarantined": N, "refused": N}.
        """
        verdicts: list[InsightVerdict] = []
        accepted = corroborated = quarantined = refused = 0

        for c in contributions:
            content = (c.get("content") or "").strip()
            if not content:
                refused += 1
                verdicts.append(InsightVerdict(content, "refused", "empty content"))
                continue

            by = c.get("by", "") or c.get("agent_id", "") or "self"
            agent_id = c.get("agent_id", "")
            rep = c.get("rep", default_rep)
            trust = float(c.get("trust", 0.5))

            # 1. Reputation gate — refuse low-rep / unknown contributors.
            if rep is None:
                refused += 1
                verdicts.append(InsightVerdict(
                    content, "refused", "reputation unknown (fail-closed)", by=by))
                continue
            if rep < self.min_rep:
                refused += 1
                verdicts.append(InsightVerdict(
                    content, "refused",
                    f"rep {rep:.0f} < min {self.min_rep:.0f}", by=by))
                continue

            # 2. Poison scan — malicious/suspicious → quarantine, never live.
            scan_level, scan_reason = self._scan(content)
            if scan_level == "quarantined":
                quarantined += 1
                node_id = None
                if writeback:
                    n = self._mesh.add(
                        content, lane="quarantine", provenance="federated-dream",
                        by=by, agent_id=agent_id, trust=0.05,
                        meta={"federated_source": c.get("source_url", ""),
                              "quarantine_reason": scan_reason,
                              "poison_scan": True})
                    node_id = n.id
                verdicts.append(InsightVerdict(
                    content, "quarantined", scan_reason, by=by,
                    trust=0.05, node_id=node_id))
                continue

            # 3. Corroboration — matching local live node → trust bump.
            corrob = self._corroborate_against_local(content)
            if corrob:
                # find the matching node's trust for the corroboration formula
                local_trust = 0.5
                key = _content_hash(content)
                for n in self._mesh._load().values():
                    if not n.superseded_by and n.lane != "quarantine" \
                            and _content_hash(n.content) == key:
                        local_trust = n.trust
                        break
                new_trust = round(1 - (1 - local_trust) * (1 - min(self.cap_trust, trust)), 4)
                corroborated += 1
                node_id = None
                if writeback:
                    n = self._mesh.add(
                        content, lane="cold", provenance="federated-dream",
                        by=by, agent_id=agent_id, trust=new_trust,
                        meta={"federated_source": c.get("source_url", ""),
                              "corroborated": True})
                    node_id = n.id
                verdicts.append(InsightVerdict(
                    content, "corroborated",
                    f"corroborates local fact (1-(1-{local_trust:.2f})(1-{trust:.2f})={new_trust:.3f})",
                    trust=new_trust, node_id=node_id, corroborated=True, by=by))
                continue

            # 4. Accepted (new, safe, gated).
            new_trust = min(self.cap_trust, trust)
            accepted += 1
            node_id = None
            if writeback:
                n = self._mesh.add(
                    content, lane="cold", provenance="federated-dream",
                    by=by, agent_id=agent_id, trust=new_trust,
                    meta={"federated_source": c.get("source_url", "")})
                node_id = n.id
            verdicts.append(InsightVerdict(
                content, "accepted", "cleared gate (new, safe)",
                trust=new_trust, node_id=node_id, by=by))

        return {
            "verdicts": [vars(v) for v in verdicts],
            "accepted": accepted,
            "corroborated": corroborated,
            "quarantined": quarantined,
            "refused": refused,
            "total": len(verdicts),
        }


__all__ = ["FederatedDream", "InsightVerdict"]
