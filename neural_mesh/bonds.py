"""Proof-of-Memory (PoM) — staked memory bonds for NEURAL_MESH.

WHY
---
The mesh's trust scalar is a floating number that compounds by corroboration
(``1-(1-t_a)(1-t_b)``) but has no economic skin in the game. Proof-of-Memory
economizes that scalar: an agent **stakes USDC behind a memory claim** (a
bond); independent corroboration earns it yield from a truth pool funded by
x402 query fees; falsification **slashes** the stake to the challenger.

The decisive design choice: **the mesh is the court.** Settlement is a pure,
deterministic function of the mesh's *own* truth detectors — no new oracle,
no vote-based judge, no trusted third party:

  * versioning      — ``superseded_by`` set        -> falsified
  * consensus       — higher-trust ``conflict_group`` contradictor -> falsified
  * poisoning       — ``lane == quarantine`` (ContentValidator)     -> falsified
  * temporal        — past-due / long-unreinforced prospective       -> falsified

This is the same machinery that already makes NEURAL_MESH beat flat vector
search (no stale truth) and resist OWASP ASI06 memory poisoning. PoM makes
"the memory you'd bet on" — Filecoin proves *storage*; PoM proves *memory
truth*.

ECONOMIC PRIMITIVES
-------------------
  * stake_claim       — lock USDC behind a claim's content root (escrow).
  * corroborate_claim — independent corroboration earns the bond yield from a
                        truth pool (funded by x402 query fees).
  * challenge_claim   — put up a counter-bond to dispute a claim.
  * settle_claim      — deterministic mesh-verdict: falsified -> slash to the
                        challenger; upheld -> slash the counter-bond to the
                        staker.
  * release_claim     — staker unwinds a still-true bond after the window.

Pure stdlib. ``dry_run`` semantics mirror x402_recall: all amounts are tracked
in micro-USDC integers and NO transaction is ever broadcast — an on-chain
escrow/slash leg is a separate, GO-gated layer (see Stage 3).

USAGE
-----
    from neural_mesh.bonds import BondLedger

    bond = BondLedger(mesh)
    bond.fund_pool(1000000)                       # $1 USDC from x402 fees
    b = bond.stake_claim("Base L2 tps is ~2", staker="agent-a", stake_usdc=100000)
    bond.corroborate_claim("Base L2 tps is ~2", corroborator="agent-b")
    verdict = bond.settle_claim(b["claim_id"])    # deterministic
"""
from __future__ import annotations

import json
import time
from typing import Any

from .core import Mesh
from .node import MemoryType
from .security import (QUARANTINE_LANE, corroboration_bump,
                       content_fingerprint)

# A bond's status lifecycle: active -> cashed (upheld, releasable) | slashed
# (falsified) | released (unwound by staker). `settled` is a transient state
# written while a settlement is in flight.
ACTIVE, CASHED, SLASHED, RELEASED = "active", "cashed", "slashed", "released"

# Default: challenge window (1 day), per-corroboration yield rate (1% of
# stake, capped by the pool), and the reinforcement horizon used by the
# temporal staleness detector (a claim not touched in this long is "stale").
DEFAULT_CHALLENGE_WINDOW = 86400.0
DEFAULT_YIELD_RATE = 0.01
DEFAULT_STALE_AFTER = 30.0 * 86400.0


def settlement_verdict(mesh: Mesh, content: str,
                       now: "float | None" = None) -> dict:
    """Deterministic, replayable truth verdict for a bonded claim.

    Reuses ONLY the mesh's own truth detectors — this is what makes PoM "no
    central oracle". Returns ``{falsified: bool, reason: str, evidence: ...}``
    where `reason` is one of:

      * ``superseded``         — a matching node was versioned over (stale truth)
      * ``quarantined``        — a matching node was flagged as poison
      * ``consensus_override`` — a higher-trust contradictor exists in the same
        ``conflict_group``
      * ``temporal_stale``     — past-due / long-unreinforced claim
      * ``unfounded``          — no node in the mesh asserts this content
      * ``upheld``             — none of the above; the claim stands

    `now` pins time so a verdict can be replayed byte-for-byte.
    """
    if now is None:
        now = time.time()
    fp = content_fingerprint(content)
    nodes = mesh._load()

    # Gather every live node asserting this exact content.
    matches = [n for n in nodes.values() if content_fingerprint(n.content) == fp]

    # 1. Unfounded: the claim has no anchor in the mesh at all.
    if not matches:
        return {"falsified": True, "reason": "unfounded",
                "evidence": None, "now": now}

    # 2. Versioning: a superseded anchor means the truth has moved on.
    for n in matches:
        if n.superseded_by:
            return {"falsified": True, "reason": "superseded",
                    "evidence": {"node": n.id, "superseded_by": n.superseded_by},
                    "now": now}

    # 3. Poisoning: quarantined anchors are falsified by definition.
    for n in matches:
        if n.lane == QUARANTINE_LANE:
            return {"falsified": True, "reason": "quarantined",
                    "evidence": {"node": n.id}, "now": now}

    # 4. Consensus: a higher-trust contradictor in the same conflict_group
    #    overrides this claim.
    for n in matches:
        if not n.conflict_group:
            continue
        for m in nodes.values():
            if (m.id == n.id or m.superseded_by
                    or m.conflict_group != n.conflict_group
                    or m.lane == QUARANTINE_LANE):
                continue
            if content_fingerprint(m.content) == fp:
                continue
            if m.trust > n.trust:
                return {"falsified": True, "reason": "consensus_override",
                        "evidence": {"node": n.id, "winner": m.id,
                                     "winner_trust": m.trust,
                                     "loser_trust": n.trust},
                        "now": now}

    # 5. Temporal staleness: a prospective claim that is past-due, or a claim
    #    left unreinforced past the horizon (and not corroborated).
    for n in matches:
        due = n.links.get("__prospective_at__")
        if due and due < now:
            return {"falsified": True, "reason": "temporal_stale",
                    "evidence": {"node": n.id, "prospective_at": due, "now": now},
                    "now": now}
        age = now - n.last_accessed
        corroborated = n.meta.get("corroborated") or ("+" in n.agent_id)
        if age > DEFAULT_STALE_AFTER and not corroborated:
            return {"falsified": True, "reason": "temporal_stale",
                    "evidence": {"node": n.id, "age_days": age / 86400.0,
                                 "corroborated": False},
                    "now": now}

    return {"falsified": False, "reason": "upheld", "evidence": None, "now": now}


class BondLedger:
    """Staked-memory bond ledger, persisted in the mesh's own SQLite file.

    Bonds are stored in a `bonds` table alongside the `nodes` table, so the
    ledger survives the same cold-start as the mesh and is wiped by the same
    destructive wipe — making the bond an *economically* load-bearing part of
    the store (the upgraded deletion gate).
    """

    def __init__(self, mesh: Mesh, *,
                 challenge_window: float = DEFAULT_CHALLENGE_WINDOW,
                 yield_rate: float = DEFAULT_YIELD_RATE):
        self.mesh = mesh
        self.challenge_window = challenge_window
        self.yield_rate = yield_rate
        self._pool = 0                       # truth pool, micro-USDC
        self._pool_row = "__truth_pool__"
        self._init_db()

    # ── persistence ──────────────────────────────────────────────────────
    def _init_db(self) -> None:
        self.mesh.db.execute(
            """CREATE TABLE IF NOT EXISTS bonds (
                claim_id     TEXT NOT NULL,
                staker       TEXT NOT NULL,
                stake_usdc   INTEGER NOT NULL,
                assertion    TEXT NOT NULL,
                bonded_at    REAL NOT NULL,
                status       TEXT NOT NULL,
                corroborators TEXT NOT NULL DEFAULT '[]',
                yield_earned_usdc INTEGER NOT NULL DEFAULT 0,
                challenged_by TEXT NOT NULL DEFAULT '',
                counter_usdc  INTEGER NOT NULL DEFAULT 0,
                settled_at   REAL,
                slash_to     TEXT NOT NULL DEFAULT '',
                verdict      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (claim_id, staker)
            )"""
        )
        self.mesh.db.execute(
            """CREATE TABLE IF NOT EXISTS bond_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        self.mesh.db.commit()
        self._pool = self._read_pool()

    def _read_pool(self) -> int:
        row = self.mesh.db.execute(
            "SELECT value FROM bond_state WHERE key = ?",
            (self._pool_row,)).fetchone()
        return int(row["value"]) if row else 0

    def _write_pool(self) -> None:
        self.mesh.db.execute(
            "REPLACE INTO bond_state (key, value) VALUES (?, ?)",
            (self._pool_row, str(self._pool)))
        self.mesh.db.commit()

    def _get(self, claim_id: str, staker: str) -> "dict | None":
        row = self.mesh.db.execute(
            "SELECT * FROM bonds WHERE claim_id = ? AND staker = ?",
            (claim_id, staker)).fetchone()
        if not row:
            return None
        b = dict(row)
        b["corroborators"] = json.loads(b.get("corroborators") or "[]")
        return b

    def _all(self) -> list[dict]:
        rows = self.mesh.db.execute("SELECT * FROM bonds").fetchall()
        out = []
        for r in rows:
            b = dict(r)
            b["corroborators"] = json.loads(b.get("corroborators") or "[]")
            out.append(b)
        return out

    def _put(self, b: dict) -> None:
        self.mesh.db.execute(
            """REPLACE INTO bonds (
                claim_id, staker, stake_usdc, assertion, bonded_at, status,
                corroborators, yield_earned_usdc, challenged_by, counter_usdc,
                settled_at, slash_to, verdict
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (b["claim_id"], b["staker"], b["stake_usdc"], b["assertion"],
             b["bonded_at"], b["status"], json.dumps(b["corroborators"]),
             b["yield_earned_usdc"], b["challenged_by"], b["counter_usdc"],
             b.get("settled_at"), b.get("slash_to", ""), b.get("verdict", "")),
        )
        self.mesh.db.commit()

    # ── the truth pool (funded by x402 query fees) ──────────────────────
    def fund_pool(self, usdc: int) -> int:
        """Credit the truth pool (micro-USDC). Returns the new pool balance."""
        self._pool += max(0, int(usdc))
        self._write_pool()
        return self._pool

    @property
    def pool(self) -> int:
        return self._pool

    # ── operations ───────────────────────────────────────────────────────
    def stake_claim(self, content: str, staker: str, stake_usdc: int,
                    assertion: str = "true") -> dict:
        """Lock USDC behind a claim. Anchors the claim as a mesh node so the
        settlement function has something to reason over.

        Returns the bond dict (with ``claim_id`` = content fingerprint).
        """
        stake = max(0, int(stake_usdc))
        claim_id = content_fingerprint(content)

        # Anchor the claim in the mesh (content-addressed, provenance=bond).
        # ContentValidator applies: poison claims are quarantined on arrival
        # and will falsify immediately at settlement — you cannot stake a
        # poison claim without it being instantly slashed.
        self.mesh.add(content, type=MemoryType.SEMANTIC, lane="cold",
                      provenance="bond", agent_id=staker, by=staker,
                      meta={"bond": {"claim_id": claim_id, "stake_usdc": stake,
                                     "staker": staker, "assertion": assertion}})

        existing = self._get(claim_id, staker)
        if existing:
            # Re-stake: top up the existing bond.
            existing["stake_usdc"] += stake
            existing["assertion"] = assertion
            self._put(existing)
            return self._public(existing)

        bond = {
            "claim_id": claim_id,
            "staker": staker,
            "stake_usdc": stake,
            "assertion": assertion,
            "bonded_at": time.time(),
            "status": ACTIVE,
            "corroborators": [],
            "yield_earned_usdc": 0,
            "challenged_by": "",
            "counter_usdc": 0,
            "settled_at": None,
            "slash_to": "",
            "verdict": "",
        }
        self._put(bond)
        return self._public(bond)

    def corroborate_claim(self, content: str, corroborator: str) -> dict:
        """Independent corroboration earns the bond yield from the truth pool.

        The corroborator's identity must differ from the staker (no
        self-corroboration). Yield is minted pro-rata (``yield_rate * stake``)
        capped by the pool balance. The corroborator also writes a mesh node,
        triggering the mesh's own cross-source corroboration bump.
        """
        claim_id = content_fingerprint(content)
        bonds = [b for b in self._all()
                 if b["claim_id"] == claim_id and b["status"] == ACTIVE]
        if not bonds:
            return {"ok": False, "error": "no active bond for this claim",
                    "claim_id": claim_id}

        results = []
        for b in bonds:
            if b["staker"] == corroborator:
                results.append({"ok": False, "error": "self-corroboration",
                                "staker": b["staker"]})
                continue
            if corroborator in b["corroborators"]:
                results.append({"ok": False, "error": "already corroborated",
                                "staker": b["staker"]})
                continue
            # Mint yield from the pool (pro-rata, capped by available pool).
            yield_usdc = min(self._pool,
                             max(1, int(b["stake_usdc"] * self.yield_rate)))
            b["yield_earned_usdc"] += yield_usdc
            b["corroborators"] = list(b["corroborators"]) + [corroborator]
            self._pool -= yield_usdc
            self._put(b)
            # Trigger the mesh's own corroboration fusion (trust bump).
            self.mesh.add(content, type=MemoryType.SEMANTIC, lane="cold",
                          provenance="bond-corroboration",
                          agent_id=corroborator, by=corroborator,
                          meta={"bond_corroboration": claim_id})
            results.append({"ok": True, "staker": b["staker"],
                            "yield_earned_usdc": yield_usdc,
                            "corroborators": len(b["corroborators"])})
        self._write_pool()
        return {"ok": True, "claim_id": claim_id, "results": results,
                "pool": self._pool}

    def challenge_claim(self, content: str, challenger: str,
                        counter_usdc: int) -> dict:
        """Put up a counter-bond to dispute a claim. Challenges require skin
        in the game so spam disputes are not free."""
        claim_id = content_fingerprint(content)
        counter = max(0, int(counter_usdc))
        bonds = [b for b in self._all()
                 if b["claim_id"] == claim_id and b["status"] == ACTIVE]
        if not bonds:
            return {"ok": False, "error": "no active bond to challenge",
                    "claim_id": claim_id}
        for b in bonds:
            b["challenged_by"] = challenger
            b["counter_usdc"] = max(b["counter_usdc"], counter)
            self._put(b)
        return {"ok": True, "claim_id": claim_id, "challenger": challenger,
                "counter_usdc": counter,
                "challenged": len(bonds)}

    def settle_claim(self, claim_id: str, staker: str = "",
                     now: "float | None" = None) -> dict:
        """Run the deterministic mesh-verdict and distribute stake.

        falsified -> staker's stake slashed to the challenger (+ corroborators
                     who earned yield keep it).
        upheld    -> challenger's counter-bond slashed to the staker; bond
                     becomes ``cashed`` (releasable).
        """
        if now is None:
            now = time.time()
        bonds = [b for b in self._all() if b["claim_id"] == claim_id]
        if staker:
            bonds = [b for b in bonds if b["staker"] == staker]
        if not bonds:
            return {"ok": False, "error": "no bond for this claim"}

        settled = []
        for b in bonds:
            if b["status"] != ACTIVE:
                settled.append({"staker": b["staker"], "ok": False,
                                "error": f"bond already {b['status']}"})
                continue
            content = self._content_for(b)
            if not content:
                verdict = {"falsified": True, "reason": "unfounded",
                           "evidence": None}
            else:
                verdict = settlement_verdict(self.mesh, content, now=now)
            if b["assertion"] in ("false", "negate", "!"):
                # Bond asserts falsehood of the content; invert the verdict.
                falsified = not verdict["falsified"]
                reason = "upheld" if falsified else verdict["reason"]
            else:
                falsified = verdict["falsified"]
                reason = verdict["reason"]

            b["settled_at"] = now
            b["verdict"] = reason
            if falsified:
                b["status"] = SLASHED
                b["slash_to"] = b["challenged_by"] or "challenger"
            else:
                b["status"] = CASHED
                b["slash_to"] = b["staker"] if b["challenged_by"] else ""
            self._put(b)
            settled.append({"staker": b["staker"], "ok": True,
                            "falsified": falsified, "reason": reason,
                            "status": b["status"],
                            "slashed_usdc": b["stake_usdc"] if falsified
                            else b["counter_usdc"]})
        return {"ok": True, "claim_id": claim_id, "settlements": settled}

    def release_claim(self, claim_id: str, staker: str = "") -> dict:
        """Unwind a cashed (upheld) bond after the challenge window."""
        bonds = [b for b in self._all() if b["claim_id"] == claim_id]
        if staker:
            bonds = [b for b in bonds if b["staker"] == staker]
        released = []
        for b in bonds:
            if b["status"] != CASHED:
                released.append({"staker": b["staker"], "ok": False,
                                 "error": f"bond is {b['status']}, not cashed"})
                continue
            b["status"] = RELEASED
            self._put(b)
            released.append({"staker": b["staker"], "ok": True,
                             "returned_usdc": b["stake_usdc"],
                             "yield_earned_usdc": b["yield_earned_usdc"]})
        return {"ok": True, "claim_id": claim_id, "released": released}

    # ── helpers ──────────────────────────────────────────────────────────
    def _content_for(self, bond: dict) -> str:
        """Recover the claim's content from the mesh (assertion="" bonds
        negate the content, so this returns the anchor text)."""
        nodes = self.mesh._load()
        for n in nodes.values():
            if content_fingerprint(n.content) == bond["claim_id"]:
                return n.content
        return ""

    def _public(self, b: dict) -> dict:
        return {k: b[k] for k in (
            "claim_id", "staker", "stake_usdc", "assertion", "bonded_at",
            "status", "corroborators", "yield_earned_usdc", "challenged_by",
            "counter_usdc", "settled_at", "slash_to", "verdict")}

    def list_bonds(self) -> list[dict]:
        return [self._public(b) for b in self._all()]

    def stats(self) -> dict:
        bonds = self._all()
        active = sum(1 for b in bonds if b["status"] == ACTIVE)
        slashed = sum(1 for b in bonds if b["status"] == SLASHED)
        cashed = sum(1 for b in bonds if b["status"] == CASHED)
        total_stake = sum(b["stake_usdc"] for b in bonds if b["status"] != RELEASED)
        total_slashed = sum(b["stake_usdc"] for b in bonds if b["status"] == SLASHED)
        return {
            "bonds": len(bonds),
            "active": active,
            "slashed": slashed,
            "cashed": cashed,
            "total_stake_usdc": total_stake,
            "total_slashed_usdc": total_slashed,
            "truth_pool_usdc": self._pool,
            "yield_rate": self.yield_rate,
            "challenge_window_sec": self.challenge_window,
        }


__all__ = [
    "BondLedger", "settlement_verdict",
    "ACTIVE", "CASHED", "SLASHED", "RELEASED",
    "DEFAULT_CHALLENGE_WINDOW", "DEFAULT_YIELD_RATE", "DEFAULT_STALE_AFTER",
]
