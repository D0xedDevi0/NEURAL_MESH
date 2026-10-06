"""Self-editing memory blocks for NEURAL_MESH (Letta-derived, mesh-native).

What this is: small, *always-in-context*, agent-writable named memory sections
with a hard character budget each. Letta's core insight is not the storage — it
is that the budget is enforced at write time, so the agent physically cannot
overflow its own always-loaded context, and that each block carries a
`description` telling the agent what belongs there.

Why it exists next to MEMORY.md/USER.md: those are edited as whole files with no
per-section budget and no edit history. This gives per-block budgets, atomic
mutations, read-only blocks, and a real version chain.

Where the state lives: in the mesh itself. A block is a mesh node with
`meta["block"] = <label>`; the *current* version is the highest `meta["version"]`
for that label, and older versions stay reachable through MESH's existing
supersedes chain. No parallel store, no duplicated versioning.

Usage:
    PYTHONPATH=. python3 -m neural_mesh.memory_blocks --db blocks.db list
    PYTHONPATH=. python3 -m neural_mesh.memory_blocks --db blocks.db render
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field

from .core import Mesh
from .node import MemoryType

DEFAULT_LIMIT = 2000


@dataclass
class Block:
    label: str
    value: str = ""
    description: str = ""
    limit: int = DEFAULT_LIMIT
    read_only: bool = False
    version: int = 1
    updated_at: float = 0.0
    node_id: str = ""
    history: list[str] = field(default_factory=list)

    @property
    def chars_current(self) -> int:
        return len(self.value)

    @property
    def chars_remaining(self) -> int:
        return self.limit - self.chars_current


class BlockLimitExceeded(ValueError):
    """A write would push the block past its character budget."""


class MemoryBlocks:
    def __init__(self, mesh: Mesh):
        self.mesh = mesh

    # ------------------------------------------------------------------ internal
    def _nodes(self, label: str) -> list:
        out = [n for n in self.mesh._load().values() if n.meta.get("block") == label]
        out.sort(key=lambda n: int(n.meta.get("version", 1)))
        return out

    def _to_block(self, node) -> Block:
        m = node.meta
        return Block(
            label=m["block"],
            value=node.content,
            description=m.get("description", ""),
            limit=int(m.get("limit", DEFAULT_LIMIT)),
            read_only=bool(m.get("read_only", False)),
            version=int(m.get("version", 1)),
            updated_at=float(m.get("updated_at", 0.0)),
            node_id=node.id,
            history=[n.id for n in self._nodes(m["block"])],
        )

    def _write(self, label: str, new_value: str, prev: Block | None) -> Block:
        """Persist a new version. Enforces the budget BEFORE writing anything."""
        limit = prev.limit if prev else DEFAULT_LIMIT
        if len(new_value) > limit:
            raise BlockLimitExceeded(
                f"block '{label}': {len(new_value)} chars exceeds limit {limit} "
                f"(by {len(new_value) - limit})"
            )
        version = (prev.version + 1) if prev else 1
        node_id = f"block:{label}:v{version}"
        self.mesh.add(
            new_value,
            type=MemoryType.SEMANTIC,
            lane="hot",  # always-in-context
            provenance="memory-blocks",
            by="agent",
            trust=1.0,
            meta={
                "block": label,
                "description": prev.description if prev else "",
                "limit": limit,
                "read_only": prev.read_only if prev else False,
                "version": version,
                "updated_at": time.time(),
                **({"supersedes": prev.node_id, "previous_value": prev.value} if prev else {}),
            },
            node_id=node_id,
        )
        return self._require(label)

    def _require(self, label: str) -> Block:
        b = self.get(label)
        if b is None:  # cannot happen after a successful write; guards the type
            raise KeyError(f"block '{label}' vanished after write")
        return b

    # ---------------------------------------------------------------------- API
    def create(
        self,
        label: str,
        value: str = "",
        description: str = "",
        limit: int = DEFAULT_LIMIT,
        read_only: bool = False,
    ) -> Block:
        if self.get(label) is not None:
            raise ValueError(f"block '{label}' already exists")
        if len(value) > limit:
            raise BlockLimitExceeded(
                f"block '{label}': {len(value)} chars exceeds limit {limit}"
            )
        self.mesh.add(
            value,
            type=MemoryType.SEMANTIC,
            lane="hot",
            provenance="memory-blocks",
            by="agent",
            trust=1.0,
            meta={
                "block": label,
                "description": description,
                "limit": limit,
                "read_only": read_only,
                "version": 1,
                "updated_at": time.time(),
            },
            node_id=f"block:{label}:v1",
        )
        return self._require(label)

    def get(self, label: str) -> Block | None:
        nodes = self._nodes(label)
        return self._to_block(nodes[-1]) if nodes else None

    def value(self, label: str) -> str:
        b = self.get(label)
        return b.value if b else ""

    def set(self, label: str, value: str) -> Block:
        prev = self.get(label)
        if prev is None:
            raise KeyError(f"no block '{label}'")
        self._guard_writable(prev)
        return self._write(label, value, prev)

    def append(self, label: str, text: str) -> Block:
        """Append, or raise — never partially write. Budget checked first."""
        prev = self.get(label)
        if prev is None:
            raise KeyError(f"no block '{label}'")
        self._guard_writable(prev)
        return self._write(label, prev.value + text, prev)

    def replace(self, label: str, old: str, new: str) -> Block:
        prev = self.get(label)
        if prev is None:
            raise KeyError(f"no block '{label}'")
        self._guard_writable(prev)
        if old not in prev.value:
            raise ValueError(f"'{old[:40]}' not found in block '{label}'")
        return self._write(label, prev.value.replace(old, new), prev)

    @staticmethod
    def _guard_writable(b: Block) -> None:
        if b.read_only:
            raise PermissionError(f"block '{b.label}' is read-only")

    def list(self) -> list[Block]:
        labels = sorted({n.meta["block"] for n in self.mesh._load().values()
                         if n.meta.get("block")})
        return [b for b in (self.get(l) for l in labels) if b]

    def history(self, label: str) -> list[Block]:
        return [self._to_block(n) for n in self._nodes(label)]

    def revert(self, label: str, version: int) -> Block:
        """Restore an earlier version's value as a NEW version (no history rewrite)."""
        prev = self.get(label)
        if prev is None:
            raise KeyError(f"no block '{label}'")
        self._guard_writable(prev)
        for b in self.history(label):
            if b.version == version:
                return self._write(label, b.value, prev)
        raise KeyError(f"block '{label}' has no version {version}")

    def render(self) -> str:
        """The always-in-context representation (Letta's XML shape)."""
        parts = ["<memory_blocks>"]
        for b in self.list():
            parts.append(f"  <{b.label}>")
            if b.description:
                parts.append(f"    <description>{b.description}</description>")
            parts.append("    <metadata>")
            parts.append(f"      chars_current={b.chars_current}")
            parts.append(f"      chars_limit={b.limit}")
            if b.read_only:
                parts.append("      read_only=true")
            parts.append("    </metadata>")
            parts.append(f"    <value>{b.value}</value>")
            parts.append(f"  </{b.label}>")
        parts.append("</memory_blocks>")
        return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Letta-style self-editing memory blocks.")
    ap.add_argument("--db", default="blocks.db")
    ap.add_argument("cmd", choices=["list", "render", "get", "history"])
    ap.add_argument("label", nargs="?")
    args = ap.parse_args(argv)

    mb = MemoryBlocks(Mesh(db_path=args.db))
    if args.cmd == "render":
        print(mb.render())
    elif args.cmd == "list":
        for b in mb.list():
            ro = " [read-only]" if b.read_only else ""
            print(f"{b.label:<20} {b.chars_current:>5}/{b.limit:<5} v{b.version}{ro}")
    elif args.cmd == "get":
        b = mb.get(args.label)
        print(json.dumps({"label": b.label, "value": b.value, "limit": b.limit,
                          "version": b.version, "chars_current": b.chars_current},
                         indent=2) if b else "not found")
    elif args.cmd == "history":
        for b in mb.history(args.label):
            print(f"v{b.version}  {b.chars_current:>5} chars  {b.value[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())