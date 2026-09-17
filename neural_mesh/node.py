"""Memory node model — the atomic unit of the mesh."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class MemoryType(str, Enum):
    WORKING = "working"          # active context window (budget-managed)
    EPISODIC = "episodic"        # what happened, when
    SEMANTIC = "semantic"        # facts about the world / user / code
    SENSORY = "sensory"          # raw inputs (summarized on ingest)
    PROCEDURAL = "procedural"    # how to do things (skills / routines)
    PROSPECTIVE = "prospective"  # intentions / futures / reminders


@dataclass
class MemoryNode:
    id: str
    type: MemoryType
    content: str
    embedding: tuple = ()
    links: dict = field(default_factory=dict)   # neighbour_id -> weight (0..1)
    created_at: float = 0.0
    last_accessed: float = 0.0
    access_count: int = 0
    resonance: float = 0.0
    trust: float = 1.0
    provenance: str = ""
    by: str = ""                    # attribution: WHO authored/recalled this
    lane: str = "hot"            # "hot" (short-term) or "cold" (long-term)
    version: int = 1
    superseded_by: str = ""
    agent_id: str = ""           # contributor agent id ("" = local/self)
    conflict_group: str = ""     # shared id linking contradictory nodes
    valid_from: float = 0.0      # when this fact became true (bi-temporal lower bound)
    valid_to: float = 0.0        # when it ceased being true (0.0 = still true / open interval)
    meta: dict = field(default_factory=dict)  # extensible provenance (e.g. Helixa stamp)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
            self.last_accessed = self.created_at
        if not self.valid_from:
            # A fact is true from when it entered the mesh unless stated otherwise.
            self.valid_from = self.created_at
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        # "by" is the human-readable attribution primitive: *remembering is BY
        # someone/thing*. Default it from structured identity if absent.
        if not self.by:
            self.by = self.agent_id or self.provenance or "self"

    def touch(self):
        self.last_accessed = time.time()
        self.access_count += 1

    def to_row(self):
        return (
            self.id,
            self.type.value,
            self.content,
            list(self.embedding),
            self.links,
            {
                "created_at": self.created_at,
                "last_accessed": self.last_accessed,
                "access_count": self.access_count,
                "resonance": self.resonance,
                "trust": self.trust,
                "provenance": self.provenance,
                "by": self.by,
                "lane": self.lane,
                "version": self.version,
                "superseded_by": self.superseded_by,
                "agent_id": self.agent_id,
                "conflict_group": self.conflict_group,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "meta": self.meta,
            },
        )

    @classmethod
    def from_row(cls, row):
        (nid, mtype, content, emb, links, meta) = row
        n = cls(
            id=nid,
            type=MemoryType(mtype),
            content=content,
            embedding=tuple(emb),
            links=dict(links),
        )
        n.created_at = meta.get("created_at", 0.0)
        n.last_accessed = meta.get("last_accessed", 0.0)
        n.access_count = meta.get("access_count", 0)
        n.resonance = meta.get("resonance", 0.0)
        n.trust = meta.get("trust", 1.0)
        n.provenance = meta.get("provenance", "")
        n.by = meta.get("by", "")
        n.lane = meta.get("lane", "hot")
        n.version = meta.get("version", 1)
        n.superseded_by = meta.get("superseded_by", "")
        n.agent_id = meta.get("agent_id", "")
        n.conflict_group = meta.get("conflict_group", "")
        n.valid_from = meta.get("valid_from", 0.0)
        n.valid_to = meta.get("valid_to", 0.0)
        n.meta = meta.get("meta", {})
        return n
