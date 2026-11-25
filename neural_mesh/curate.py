"""Fact curation — the ingest layer the mesh was missing.

WHY
---
The measured failure mode of every memory system we benchmarked was not
retrieval quality, it was *what never got written down*. A mesh that answers
90% of curated facts still answers 5% of the questions whose facts were never
ingested. The providers that do better at this (Mem0's ADD/UPDATE/DELETE/NOOP
decisioning, ByteRover's pre-compression extraction) share one shape:

    raw turn / note  ->  candidate facts  ->  per-fact lifecycle decision
                     ->  write only what the store does not already know

This module implements that shape deterministically. No LLM, no network, no
new dependency: extraction is rule-based, and the decision is a comparison
against what the mesh already holds. An optional `extractor` callable lets a
caller plug an LLM in front of the same decision engine.

Decisions
---------
    ADD       fact is new -> write it
    UPDATE    same subject slot, different value -> write it with `supersedes`
    NOOP      already stored (or a lower-trust duplicate) -> do nothing
    REJECT    not a durable fact (question, code, path noise, too short)
    QUARANTINE handled by Mesh.add's ContentValidator, reported here

Trust rule (the one that matters)
---------------------------------
An UPDATE never replaces a memory that is *more* trusted than the incoming
fact. A low-trust paraphrase cannot overwrite a corroborated fact; it is
reported as NOOP with reason ``lower-trust``. Silence here would be a
memory-poisoning path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .node import MemoryType

ADD, UPDATE, NOOP, REJECT = "ADD", "UPDATE", "NOOP", "REJECT"

# Lines that are never durable facts.
_NOISE = re.compile(
    r"^(```|\{|\}|\[|#|//|--|\||>|\d+[.)]\s|[-*]\s*$)|"
    r"(^\s*(ls|cd|git|npm|pip|python|curl|ssh|docker)\s)|"
    r"(^\s*[\w./-]+\.(py|js|ts|json|yaml|yml|md|db|so|log)\s*$)", re.I)
_QUESTION = re.compile(r"\?\s*$")
_IMPERATIVE_START = re.compile(
    r"^(remember|note|keep in mind|always|never|use|avoid|prefer|don't|do not)\b", re.I)
_FILLER = re.compile(
    r"^(?:so|okay|ok|well|also|and|but|anyway|basically|actually|"
    r"just to confirm|for the record|note that|remember that)\b[,: ]*", re.I)
_TRAILING = re.compile(r"[\s.;,]+$")
_CODE_FENCE = re.compile(r"```.*?```", re.S)


@dataclass
class Decision:
    fact: str
    action: str
    node_id: str = ""
    supersedes: str = ""
    reason: str = ""
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"fact": self.fact, "action": self.action,
                "node_id": self.node_id, "supersedes": self.supersedes,
                "reason": self.reason, "score": round(self.score, 4)}


@dataclass
class CurationReport:
    decisions: list = field(default_factory=list)

    def _n(self, action: str) -> int:
        return sum(1 for d in self.decisions if d.action == action)

    @property
    def added(self) -> int:
        return self._n(ADD)

    @property
    def updated(self) -> int:
        return self._n(UPDATE)

    @property
    def skipped(self) -> int:
        return self._n(NOOP)

    @property
    def rejected(self) -> int:
        return self._n(REJECT)

    def summary(self) -> dict:
        return {"added": self.added, "updated": self.updated,
                "skipped": self.skipped, "rejected": self.rejected,
                "total": len(self.decisions)}

    def as_dict(self) -> dict:
        return {"summary": self.summary(),
                "decisions": [d.as_dict() for d in self.decisions]}


def normalize(text: str) -> str:
    """Identity form used for duplicate detection (not for storage)."""
    t = _FILLER.sub("", " ".join((text or "").split()))
    t = _TRAILING.sub("", t)
    return t.lower().strip()


_WORD = re.compile(r"[a-z0-9][a-z0-9_.:/+-]*")


def tokens(text: str) -> set:
    return set(_WORD.findall(normalize(text)))


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_STOP = {"the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on",
         "for", "and", "or", "it", "this", "that", "with", "from", "by", "as",
         "at", "about", "into", "your", "our", "its"}


def rare_tokens(text: str) -> set:
    """Tokens that carry identity: long words, numbers, paths, ids.

    Used to tell a *restatement* ("the wallet key is in X" vs "the wallet key
    lives at X") from a *change* ("port 8787" vs "port 8788").
    """
    out = set()
    for w in _WORD.findall(normalize(text)):
        if w in _STOP:
            continue
        if len(w) >= 4 or any(c.isdigit() for c in w) or any(c in w for c in "./:-_"):
            out.add(w)
    return out


def rare_overlap(a: str, b: str) -> float:
    ra, rb = rare_tokens(a), rare_tokens(b)
    if not ra or not rb:
        return 0.0
    return len(ra & rb) / len(ra | rb)


# Values start with a digit, a version marker or a URL — but a name like
# ``d0xeddev`` is a subject, not a value, so only the *leading* char decides.
_VALUE_TOKEN = re.compile(r"^(\d|https?://|www\.)", re.I)
_VERB_SPLIT = re.compile(
    r"\b(is|are|was|were|has|have|had|uses|runs|means|lives|lived|prefers|"
    r"costs|requires|deploys|owns|contains|points|sits|stores)\b", re.I)


def subject_key(text: str) -> str:
    """The 'slot' a fact occupies — subject side only, never the value.

    ``The Base RPC endpoint is mainnet.base.org`` and ``The Base RPC endpoint
    is https://…`` share the slot ``base rpc endpoint``, so the second is an
    UPDATE of the first rather than an unrelated new memory.
    """
    n = normalize(text)
    m = _VERB_SPLIT.search(n)
    head = n[:m.start()] if m else n
    # Values (numbers, versions, ids) belong to the value side, never the slot:
    # "write_file caps at 5.5 kb" and "... at 4 kb" share one slot.
    words = [w for w in _WORD.findall(head)
             if w not in _STOP and not _VALUE_TOKEN.match(w)]
    if not words:
        words = [w for w in _WORD.findall(n)
                 if w not in _STOP and not _VALUE_TOKEN.match(w)]
    return " ".join(words[:3])


def is_fact_like(line: str) -> bool:
    t = (line or "").strip()
    if len(t) < 18 or len(t) > 400:
        return False
    if _QUESTION.search(t) or _NOISE.search(t):
        return False
    # a bare URL or filesystem path is a pointer, not a fact
    if re.match(r"^(https?://|/|\./|~/|\w+://)", t) and " " not in t:
        return False
    if t.startswith((">", "|")) or t.count("`") >= 2:
        return False
    # a fact needs at least one verb-ish or assignment structure
    if not re.search(r"\b(is|are|was|were|has|have|uses|runs|means|"
                     r"lives|lived|prefers|costs|requires|deploys|owns|"
                     r"caps|holds|keeps|lists|maps|listens|reaches|supports|"
                     r"takes|lasts|fits|tracks|powers|serves|hosts|contains|"
                     r"points|sits|stores|becomes|stays)\b|[:=]", t, re.I):
        if not _IMPERATIVE_START.match(t):
            return False
    return True
QUARANTINE = "QUARANTINE"
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def extract_facts(text: str, *, max_facts: int = 200) -> list:
    """Deterministic fact candidates from a turn, note block or transcript.

    Splits on lines and sentence boundaries, strips bullets/filler, drops
    questions, code and path noise, and de-duplicates within the batch. This
    is deliberately conservative: a missed fact is better than a stored blob.
    """
    text = _CODE_FENCE.sub(" ", text or "")
    out: list = []
    seen: set = set()
    for raw_line in text.splitlines():
        line = _BULLET.sub("", raw_line.strip())
        if not line:
            continue
        parts = [line]
        if len(line) > 220:
            parts = [p.strip() for p in re.split(r"(?<=[.!])\s+", line)]
        for part in parts:
            candidate = _FILLER.sub("", part).strip(" -—•*`\"'")
            candidate = _TRAILING.sub("", candidate)
            if not is_fact_like(candidate):
                continue
            key = normalize(candidate)
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= max_facts:
                return out
    return out


class Curator:
    """Turn raw text into lifecycle decisions against a live Mesh."""

    def __init__(self, mesh, *, update_slot_sim: float = 0.35,
                 dup_sim: float = 0.90, near_dup_rare: float = 0.70,
                 candidate_k: int = 8):
        self.mesh = mesh
        self.update_slot_sim = update_slot_sim
        self.dup_sim = dup_sim
        self.near_dup_rare = near_dup_rare
        self.candidate_k = candidate_k

    # -- decision ----------------------------------------------------------
    def decide(self, fact: str, *, kind: str = "semantic", by: str = "self",
               source: str = "", trust: float = 1.0) -> Decision:
        if not is_fact_like(fact):
            return Decision(fact, REJECT, reason="not-fact-like")
        norm = normalize(fact)
        slot = subject_key(fact)
        try:
            candidates = self.mesh.hybrid_recall(fact, top_k=self.candidate_k)
        except Exception:
            candidates = []
        best = None
        best_score = 0.0
        for node in candidates:
            cnorm = normalize(getattr(node, "content", ""))
            if cnorm == norm:
                return Decision(fact, NOOP, node_id=node.id,
                                reason="exact-duplicate", score=1.0)
            sim = jaccard(fact, node.content)
            slot_hit = slot and slot == subject_key(node.content)
            score = sim + (0.25 if slot_hit else 0.0)
            if score > best_score:
                best, best_score = node, score
            if sim >= self.dup_sim:
                return Decision(fact, NOOP, node_id=node.id,
                                reason="near-duplicate", score=sim)
            # A restatement with the same identity tokens is a duplicate, not a
            # correction. Without this, a harmless rephrase SUPERSEDES the
            # original and the stored wording is silently replaced.
            if rare_overlap(fact, node.content) >= self.near_dup_rare:
                return Decision(fact, NOOP, node_id=node.id,
                                reason="restatement", score=sim)
        if best is not None and best_score >= self.update_slot_sim and \
                slot == subject_key(best.content) and norm != normalize(best.content):
            if float(getattr(best, "trust", 1.0) or 1.0) > float(trust) + 0.05:
                return Decision(fact, NOOP, node_id=best.id,
                                reason="lower-trust", score=best_score)
            return Decision(fact, UPDATE, supersedes=best.id,
                            reason="slot-update", score=best_score)
        return Decision(fact, ADD, reason="new")

    # -- execution ---------------------------------------------------------
    def ingest(self, text: str, *, kind: str = "semantic", by: str = "self",
               source: str = "", trust: float = 1.0, dry_run: bool = False,
               extractor=None) -> CurationReport:
        facts = extractor(text) if extractor else extract_facts(text)
        report = CurationReport()
        for fact in facts:
            decision = self.decide(fact, kind=kind, by=by, source=source,
                                   trust=trust)
            if decision.action in (ADD, UPDATE) and not dry_run:
                node = self.mesh.add(
                    fact, type=MemoryType(kind), by=by, provenance=source, trust=trust,
                    supersedes=decision.supersedes,
                    meta={"curated": True, "curator": "deterministic",
                          "level": "explicit", "decision": decision.action,
                          "source": source} if source else
                         {"curated": True, "curator": "deterministic",
                          "level": "explicit", "decision": decision.action},
                )
                decision.node_id = node.id
                if getattr(node, "lane", "") == "quarantine":
                    decision.action = QUARANTINE
                    decision.reason = "validator-quarantined"
            report.decisions.append(decision)
        return report


def curate_memory_file(mesh, path: str, *, by: str = "self",
                       dry_run: bool = False) -> CurationReport:
    """Ingest a MEMORY.md-style file: ``§``/blank-line separated entries."""
    from pathlib import Path
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*§\s*\n|\n{2,}", text)
    report = CurationReport()
    curator = Curator(mesh)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        sub = curator.ingest(block, by=by, source=path, dry_run=dry_run)
        report.decisions.extend(sub.decisions)
    return report
