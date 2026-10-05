"""Session → lesson distillation for NEURAL_MESH.

The other half of the Cognee borrow (the first half is `code_ingest`): turn
*accepted session lessons* into first-class mesh memory, then surface the ones
that recur across sessions as skill-promotion candidates.

Why in the mesh and not a text file: lessons are claims with provenance that
should be retrievable next to the facts they qualify, deduped against what the
mesh already knows, and eligible for DREAM consolidation. A markdown pile is
none of those.

Pipeline:
    extract  — pull rule-like lines out of transcripts (heuristic, no LLM)
    distill  — dedupe against existing nodes, add the rest as PROCEDURAL nodes
    report   — group by signature; support >= N distinct sources = promote

Propose-only by contract: `report` never writes to a skill. Promotion is a
decision for the agent/human (same discipline as the dream-cycle staging).

Usage:
    PYTHONPATH=. python3 -m neural_mesh.session_distill --yantrikdb --db lessons.db
    PYTHONPATH=. python3 -m neural_mesh.session_distill path/to/transcript.md --db lessons.db
    PYTHONPATH=. python3 -m neural_mesh.session_distill --report --db lessons.db
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from .core import Mesh
from .node import MemoryType

DEFAULT_YANTRIKDB = "/opt/data/yantrikdb/memory.db"

# Rule-like line markers. Kept intentionally narrow — precision over recall,
# because a wrong lesson in memory is worse than a missing one.
MARKERS = (
    "rule:", "rules:", "pitfall:", "pitfalls:", "gotcha:", "gotchas:",
    "lesson:", "lessons:", "note:", "important:", "warning:", "caveat:",
    "never:", "always:", "must:", "do not:", "don't:", "remember:",
)
# Imperative openers that usually carry a durable lesson even without a colon.
IMPERATIVE = re.compile(
    r"^(never|always|do not|don't|avoid|prefer|use|keep|make sure|ensure|"
    r"remember to|don't forget|beware|watch out)\b",
    re.I,
)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+\.)\s+")

# Lines that look rule-like but are noise (too short, pure code, URLs).
NOISE = re.compile(r"^[\s`{}()\[\]<>/\\|#=-]+$|^https?://")
# Status/narration phrasing that rides along in transcripts but carries no
# durable rule. Dropped so the mesh isn't polluted with "this session couldn't
# run X" lines that are stale by the next session.
STATUS_NOISE = re.compile(
    r"\b(this session|this draft|this is a draft|awaiting|for review|"
    r"no shell tool|no terminal tool|couldn'?t run|unavailable in this|"
    r"please archive|qc)\b", re.I)
# Bare file paths / identifiers with no prose.
PATHLIKE = re.compile(r"^[\w@./:-]+$")

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "it", "its", "this", "that", "with", "as", "at", "by", "from", "into",
    "not", "no", "you", "your", "we", "our", "if", "then", "when", "so", "but",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t not in _STOP]


def signature(text: str) -> str:
    """Order-insensitive normalised key used for recurrence counting."""
    return " ".join(sorted(set(_tokens(text))))


def jaccard(a: str, b: str) -> float:
    sa, sb = set(_tokens(a)), set(_tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def extract_lessons(text: str) -> list[str]:
    """Pull candidate lesson lines from a transcript block."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or NOISE.match(line):
            continue
        body = line
        marker = ""
        for m in MARKERS:
            idx = line.lower().find(m)
            if idx != -1:
                marker = m
                body = line[idx + len(m):].strip() or line
                break
        bulleted = bool(BULLET.match(raw))
        if not marker:
            stripped = BULLET.sub("", line)
            if bulleted and IMPERATIVE.match(stripped):
                body = stripped
            elif not (IMPERATIVE.match(stripped) and len(stripped) > 20):
                continue
            else:
                body = stripped
        body = body.strip(" -—•*`")
        if len(body) < 20 or len(body) > 400:
            continue
        if body.endswith("?") or body.startswith(("```", "{")):
            continue
        if STATUS_NOISE.search(body) or PATHLIKE.match(body):
            continue
        if body not in out:
            out.append(body)
    return out


def yantrikdb_turns(db_path: str = DEFAULT_YANTRIKDB) -> list[tuple[str, str]]:
    """Return (source_id, content) for conversation turns, grouped by namespace."""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = list(con.execute(
            "SELECT namespace, content FROM conversation_turns "
            "WHERE content IS NOT NULL AND length(content) > 0"))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [(ns or "unknown", content) for ns, content in rows]


class SessionDistiller:
    def __init__(self, mesh: Mesh, dup_threshold: float = 0.72):
        self.mesh = mesh
        self.dup_threshold = dup_threshold
        self.stats = {"extracted": 0, "added": 0, "duplicates": 0, "sources": 0}

    def _existing_conflict(self, lesson: str):
        """Return the existing lesson node this duplicates, else None.

        Token-Jaccard rather than embedding distance: the default hashed
        embedder is coarse, and duplicates here are near-verbatim, not
        paraphrases. Deterministic and cheap.
        """
        for n in self.mesh._load().values():
            if n.meta.get("lesson") and jaccard(lesson, n.content) >= self.dup_threshold:
                return n
        return None

    def distill(self, items: list[tuple[str, str]]) -> dict:
        """items = [(source_id, transcript_text)]. Adds new lessons to the mesh.

        A repeated lesson is NOT written twice: it stays one node and gains the
        new source. Support (how many distinct sources assert the lesson) is the
        recurrence signal `report()` promotes on — deduping without recording
        the source would silently destroy it.

        Stats are per-call (this returns what THIS batch did), so an accumulating
        counter can't be mistaken for dedupe working.
        """
        self.stats = {"extracted": 0, "added": 0, "duplicates": 0, "sources": 0}
        for source, text in items:
            lessons = extract_lessons(text)
            self.stats["extracted"] += len(lessons)
            added_any = False
            for lesson in lessons:
                existing = self._existing_conflict(lesson)
                if existing is not None:
                    srcs = existing.meta.get("sources")
                    if not isinstance(srcs, list):
                        srcs = [existing.meta.get("source", "?")]
                    if source not in srcs:
                        srcs.append(source)
                        existing.meta["sources"] = srcs
                        existing.meta["support"] = len(srcs)
                        self.mesh._save(existing)
                    self.stats["duplicates"] += 1
                    continue
                self.mesh.add(
                    lesson,
                    type=MemoryType.PROCEDURAL,
                    lane="cold",
                    provenance="session-distill",
                    by="distill",
                    trust=0.8,
                    meta={
                        "lesson": True,
                        "source": source,
                        "sources": [source],
                        "support": 1,
                        "signature": signature(lesson),
                    },
                )
                self.stats["added"] += 1
                added_any = True
            if added_any:
                self.stats["sources"] += 1
        return self.stats

    def report(self, min_support: int = 2) -> list[dict]:
        """Lessons asserted by >= min_support distinct sources.

        Propose-only: these are skill-promotion candidates, not promotions.
        """
        out: list[dict] = []
        for n in self.mesh._load().values():
            if not n.meta.get("lesson"):
                continue
            srcs = n.meta.get("sources")
            if not isinstance(srcs, list) or not srcs:
                srcs = [n.meta.get("source", "?")]
            if len(srcs) >= min_support:
                out.append({
                    "signature": n.meta.get("signature") or signature(n.content),
                    "support": len(srcs),
                    "sources": sorted(srcs)[:6],
                    "example": n.content,
                })
        out.sort(key=lambda r: -r["support"])
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Distill session lessons into NEURAL_MESH.")
    ap.add_argument("paths", nargs="*", help="transcript files/dirs to distill")
    ap.add_argument("--db", default="lessons.db")
    ap.add_argument("--yantrikdb", action="store_true",
                    help=f"also read conversation turns from {DEFAULT_YANTRIKDB}")
    ap.add_argument("--report", action="store_true", help="only print promotion candidates")
    ap.add_argument("--min-support", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    mesh = Mesh(db_path=args.db)
    d = SessionDistiller(mesh)

    if args.report:
        rows = d.report(args.min_support)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"🟦 promotion candidates (support >= {args.min_support}): {len(rows)}")
            for r in rows[:30]:
                print(f"  [{r['support']}] {r['example'][:150]}")
        return 0

    items: list[tuple[str, str]] = []
    if args.yantrikdb:
        items.extend(yantrikdb_turns())
    for p in args.paths:
        path = Path(p)
        files = [path] if path.is_file() else sorted(path.rglob("*.md"))
        for f in files:
            items.append((f.name, f.read_text(encoding="utf-8", errors="replace")))

    if not items:
        print("nothing to distill (pass paths or --yantrikdb)")
        return 1

    stats = d.distill(items)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"🟦 distilled {stats['extracted']} lesson candidates from {len(items)} sources")
        print(f"  added {stats['added']} new | {stats['duplicates']} duplicate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())