"""Cross-mesh federation client — discover, query, and merge from peer meshes.

A ``PeerClient`` represents one remote NEURAL_MESH instance.  It queries the
peer manifest to decide whether to trust that mesh, and then issues recall
and subgraph queries for cross-agent memory retrieval.

Usage::

    from neural_mesh.peer import PeerClient, discover_peer

    peer = discover_peer("https://peer-mesh.example.com")
    print(peer.manifest["nodes"], "nodes available at", peer.base_url)

    results = peer.recall("agent memory", top_k=5, mode="resonance")
    for r in results:
        print(r["id"], r["trust"], r["content"][:60])
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

__all__ = ["PeerClient", "discover_peer", "PeerError"]


class PeerError(Exception):
    """Raised when the peer mesh returns an error or is unreachable."""


class PeerClient:
    """Thin HTTP client for a federated NEURAL_MESH peer."""

    def __init__(self, base_url: str, token: str | None = None):
        """*base_url* — scheme + host (e.g. ``https://api.d0xeddev.com``).
        *token* — API token for auth-protected endpoints (peer query, merge).
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.manifest: dict[str, Any] = {}

    # ── HTTP helpers ─────────────────────────────────────────────────────
    def _build_request(self, method: str, path: str, body: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = self.base_url + path
        data = None
        if body is not None:
            data = json.dumps(body).encode()
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def _send(self, method: str, path: str, body: dict | None = None) -> dict:
        req = self._build_request(method, path, body)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            msg = f"peer HTTP {e.code} on {path}"
            try:
                body = json.loads(e.read())
                msg += f": {body.get('error', str(body))}"
            except Exception:
                pass
            raise PeerError(msg) from e
        except urllib.error.URLError as e:
            raise PeerError(f"peer unreachable {path}: {e.reason}") from e

    # ── Public API ───────────────────────────────────────────────────────
    def discover(self) -> dict:
        """FETCH the peer manifest and store it.
        Returns the manifest dict.  Idempotent: call again to refresh.
        """
        self.manifest = self._send("GET", "/mesh/peer/manifest")
        return self.manifest

    def recall(self, query: str, top_k: int = 5, lane: str | None = None,
               mode: str = "resonance") -> list[dict]:
        """Query the peer mesh like any local recall.

        * *query* — natural-language query.
        * *top_k* — max results (1..50).
        * *lane* — ``"hot"``, ``"cold"``, or ``None`` (all).
        * *mode* — ``"resonance"``, ``"dense"``, ``"lexical"``, ``"hybrid"``.

        Returns list of result dicts with id, content, trust, lane,
        provenance, agent_id, conflict_group, helixa stamp.
        """
        body = {"query": query, "top_k": top_k, "mode": mode}
        if lane is not None:
            body["lane"] = lane
        resp = self._send("POST", "/mesh/peer/query", body)
        return resp.get("results", [])

    def subgraph(self, *, lane: str | None = None, provenance: str | None = None,
                 by: str | None = None, since: float | None = None,
                 trust_min: float | None = None, trust_max: float | None = None,
                 limit: int = 50) -> list[dict]:
        """Structured subgraph query on the peer mesh.

        All filters are optional and combined with AND.  See
        ``POST /mesh/subgraph`` for filter semantics.
        """
        body: dict[str, Any] = {"limit": limit}
        if lane is not None:
            body["lane"] = lane
        if provenance is not None:
            body["provenance"] = provenance
        if by is not None:
            body["by"] = by
        if since is not None:
            body["since"] = since
        if trust_min is not None:
            body["trust_min"] = trust_min
        if trust_max is not None:
            body["trust_max"] = trust_max
        resp = self._send("POST", "/mesh/subgraph", body)
        return resp.get("results", [])

    def stats(self) -> dict:
        """Shortcut: get /mesh/stats (public)."""
        return self._send("GET", "/mesh/stats")

    def health(self) -> dict:
        """Shortcut: get /health."""
        return self._send("GET", "/health")

    def paid_recall(self, query: str, *, tier: str = "basic",
                    proof_header: str = "", top_k: int = 5,
                    mode: str = "resonance") -> dict:
        """Pay-gated recall on the peer mesh (x402).

        Sends the payment proof as ``X-Payment-Proof`` and the tier as
        ``X-Recall-Tier`` headers to ``POST /mesh/recall-paid``. The peer verifies
        the receipt on-chain (or in dry-run) before returning results.
        """
        headers = {"Content-Type": "application/json"}
        body = {"query": query, "top_k": top_k, "mode": mode}
        if proof_header:
            headers["X-Payment-Proof"] = proof_header
        if tier:
            headers["X-Recall-Tier"] = tier
        url = self.base_url + "/mesh/recall-paid"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            msg = f"peer HTTP {e.code} on /mesh/recall-paid"
            try:
                body = json.loads(e.read())
                msg += f": {body.get('error', str(body))}"
            except Exception:
                pass
            raise PeerError(msg) from e
        except urllib.error.URLError as e:
            raise PeerError(f"peer unreachable /mesh/recall-paid: {e.reason}") from e

    def reputation(self, tag: str = "starred", agent_id: str = "") -> dict:
        """Fetch the peer's ERC-8004 reputation signal (public feed).

        Returns the raw JSON from ``GET /mesh/erc8004/reputation`` — a dict with
        ``value`` / ``tag1`` / ``agent_id`` when the peer exposes it.
        """
        path = f"/mesh/erc8004/reputation?tag1={tag}"
        if agent_id:
            path += f"&agent_id={agent_id}"
        return self._send("GET", path)


def discover_peer(base_url: str, token: str | None = None) -> PeerClient:
    """Convenience: create a PeerClient and fetch its manifest in one call."""
    peer = PeerClient(base_url, token)
    peer.discover()
    return peer
