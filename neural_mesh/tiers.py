"""Tiered memory rendering — L0 gist / L1 brief / L2 full.

WHY
---
Injected memory competes with the actual task for context. Measured on real
ops questions, a flat "first 400 chars of every hit" policy spent 750-930
chars per query and still dragged whole compaction summaries into the window.
Tiered loading (the OpenViking L0/L1/L2 idea, reimplemented here) makes depth a
budget decision instead of a constant:

    L0  one-clause gist      ~140 chars   "is this memory even about it?"
    L1  key sentences        ~420 chars   enough to act on
    L2  the full stored fact  as written   when precision matters

`tiered_recall` assigns depth by rank under a hard character budget: the top
hit gets the deepest level that fits, the tail degrades to gists, and nothing
is ever silently cut mid-word without an explicit ellipsis.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LEVELS = ("L0", "L1", "L2")
_LIMITS = {"L0": 140, "L1": 420}


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def gist(text: str, limit: int = 140) -> str:
    """First clause/sentence, hard-trimmed at a word boundary + ellipsis."""
    t = _clean(text)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in (". ", "; ", " — ", ", "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[:idx].rstrip() + "…"
    return cut.rsplit(" ", 1)[0].rstrip() + "…"


def brief(text: str, limit: int = 420) -> str:
    """Up to the first two sentences that fit, else a longer gist."""
    t = _clean(text)
    if len(t) <= limit:
        return t
    sentences = [s.strip() for s in t.replace("! ", ". ").replace("? ", ". ")
                 .split(". ") if s.strip()]
    out = ""
    for s in sentences[:2]:
        candidate = (out + ". " + s).strip(". ") if out else s
        if len(candidate) + 1 > limit:
            break
        out = candidate
    if out and len(out) >= 40:
        return (out + ".") if not out.endswith(".") else out
    return gist(t, limit)


def render_node(node, level: str = "L2") -> str:
    """One memory at a requested depth. Unknown levels fall back to L1."""
    content = getattr(node, "content", str(node))
    level = level if level in LEVELS else "L1"
    if level == "L0":
        return gist(content, _LIMITS["L0"])
    if level == "L1":
        return brief(content, _LIMITS["L1"])
    return _clean(content)


@dataclass
class TieredContext:
    items: list = field(default_factory=list)   # [(node, level, text)]
    used_chars: int = 0
    budget_chars: int = 0

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def levels(self) -> dict:
        out: dict = {}
        for _, level, _ in self.items:
            out[level] = out.get(level, 0) + 1
        return out

    def render(self, *, numbered: bool = False) -> str:
        lines = []
        for i, (_, _, text) in enumerate(self.items, 1):
            if numbered:
                lines.append(f"{i}. {text}")
            else:
                lines.append(f"- {text}")
        return "\n".join(lines)


def _default_level(rank: int, total: int) -> str:
    if rank == 0:
        return "L2"
    if rank < 3:
        return "L1"
    return "L0"


def tier_nodes(nodes, *, budget_chars: int = 1200,
               level: str = "auto") -> TieredContext:
    """Tier an already-retrieved node list under a hard character budget.

    ``level`` may be ``"auto"`` (depth by rank) or a fixed ``L0``/``L1``/``L2``.
    Degradation is graceful: if the preferred depth does not fit, the same
    memory is offered at the next shallower level rather than dropped.
    """
    hits = list(nodes or [])
    ctx = TieredContext(budget_chars=budget_chars)
    used = 0
    for rank, node in enumerate(hits):
        preferred = level if level in LEVELS else _default_level(rank, len(hits))
        ladder = [preferred] + [lv for lv in ("L1", "L0")
                                if LEVELS.index(lv) < LEVELS.index(preferred)]
        chosen = None
        for lv in ladder:
            text = render_node(node, lv)
            if not text:
                continue
            if used + len(text) + 2 <= budget_chars:
                chosen = (lv, text)
                break
        if chosen is None:
            continue
        ctx.items.append((node, chosen[0], chosen[1]))
        used += len(chosen[1]) + 2
    ctx.used_chars = used
    return ctx


def tiered_recall(mesh, query: str, top_k: int = 5, *,
                  budget_chars: int = 1200, level: str = "auto",
                  rel_floor: float = 0.0) -> TieredContext:
    """Retrieve from ``mesh`` and tier the hits (see ``tier_nodes``)."""
    hits = list(mesh.hybrid_recall(query, top_k=top_k, rel_floor=rel_floor) or [])
    return tier_nodes(hits, budget_chars=budget_chars, level=level)
