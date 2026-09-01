"""Proof-of-Memory economics ablation — the honest "cost to lie" number.

Measures the ONE thing staked memory changes: the price of asserting a false
claim. Across N seeded scenarios (a known-truth market where an "oracle" holds
the true price and a liar asserts a wrong one):

  WITH bond    — the liar must STAKE USDC behind the lie. When the challenger
                 supersedes it (adds the true price) and settles, the stake is
                 SLASHED. Lying costs the stake.
  WITHOUT bond — the liar writes the false node for free. No economic
                 consequence (the challenger may supersede it out of hygiene,
                 but the liar paid nothing).

The honest, reproducible headline is therefore: **cost to lie > 0 with a bond,
== 0 without one** — bonded memory makes false claims net-negative.

Emits `docs/assets/bond_economics.svg` (pure-stdlib bar figure) and
`data/bond_economics.json`. Zero external deps.

Run:
    PYTHONPATH=. python3 bench/bond_economics.py [--n 50] [--seed 1337]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neural_mesh import Mesh
from neural_mesh.bonds import BondLedger
from neural_mesh.node import MemoryType

REPO = Path(__file__).resolve().parent.parent


def simulate_scenario(rng: random.Random, with_bond: bool,
                      stake_usdc: int, counter_usdc: int) -> dict:
    """One lie-and-correct cycle. Returns measured economics."""
    mesh = Mesh(":memory:")
    truth_price = rng.randint(90, 110)
    lie_price = truth_price + rng.choice([-40, -25, 25, 40, 99])
    truth = f"asset price = {truth_price}"
    lie = f"asset price = {lie_price}"

    # The oracle anchors the true price (high-trust, corroborated).
    mesh.add(truth, type=MemoryType.SEMANTIC, lane="cold", agent_id="oracle",
             trust=1.0, provenance="oracle")

    if with_bond:
        ledger = BondLedger(mesh)
        ledger.fund_pool(10 * stake_usdc)
        bond = ledger.stake_claim(lie, staker="liar", stake_usdc=stake_usdc)
        # The challenger adds the superseding truth (the falsifying evidence),
        # then disputes the lie.
        lie_node = [n for n in mesh._load().values() if n.content == lie][0]
        mesh.add(truth, type=MemoryType.SEMANTIC, lane="cold", agent_id="challenger",
                 trust=1.0, provenance="challenger", supersedes=lie_node.id)
        ledger.challenge_claim(lie, challenger="challenger",
                               counter_usdc=counter_usdc)
        s = ledger.settle_claim(bond["claim_id"])
        r = s["settlements"][0]
        falsified = r["falsified"]
        return {
            "with_bond": True,
            "falsified": falsified,
            "reason": r["reason"],
            "liar_cost_usdc": r["slashed_usdc"] if falsified else -counter_usdc,
            "challenger_reward_usdc": r["slashed_usdc"] if falsified else 0,
            "lie_survives": not falsified,
        }

    # Without bond: the liar writes the lie for free; the challenger
    # supersedes it out of hygiene. No stake changes hands.
    lie_node = mesh.add(lie, type=MemoryType.SEMANTIC, lane="cold",
                        agent_id="liar", trust=0.6)
    mesh.add(truth, type=MemoryType.SEMANTIC, lane="cold", agent_id="challenger",
             trust=1.0, supersedes=lie_node.id)
    return {
        "with_bond": False,
        "falsified": True,           # corrected by hygiene, but...
        "reason": "superseded (free)",
        "liar_cost_usdc": 0,          # ...the liar paid nothing
        "challenger_reward_usdc": 0,
        "lie_survives": False,
    }


def run_ablation(n: int = 50, seed: int = 1337,
                 stake_usdc: int = 100_000,
                 counter_usdc: int = 20_000) -> dict:
    """Run the with/without arms and aggregate the honest economics."""
    rng = random.Random(seed)
    with_bond, without_bond = [], []
    for _ in range(n):
        with_bond.append(simulate_scenario(rng, True, stake_usdc, counter_usdc))
        without_bond.append(simulate_scenario(rng, False, stake_usdc, counter_usdc))

    def agg(rows: list[dict]) -> dict:
        falsified = sum(1 for r in rows if r["falsified"])
        cost = [r["liar_cost_usdc"] for r in rows]
        reward = [r["challenger_reward_usdc"] for r in rows]
        return {
            "n": len(rows),
            "falsified": falsified,
            "survived": len(rows) - falsified,
            "mean_liar_cost_usdc": round(sum(cost) / len(cost), 2),
            "mean_challenger_reward_usdc": round(sum(reward) / len(reward), 2),
            "total_slashed_usdc": sum(r["challenger_reward_usdc"] for r in rows),
        }

    return {
        "seed": seed,
        "n": n,
        "stake_usdc": stake_usdc,
        "counter_usdc": counter_usdc,
        "with_bond": agg(with_bond),
        "without_bond": agg(without_bond),
    }


def render_svg(result: dict, path: Path) -> None:
    """Pure-stdlib bar figure: liar cost with vs without a bond."""
    wb = result["with_bond"]
    wob = result["without_bond"]
    max_cost = max(wb["mean_liar_cost_usdc"], wob["mean_liar_cost_usdc"], 1)
    # scale to 0..1 for bar height
    h_wb = 0.05 + 0.9 * (wb["mean_liar_cost_usdc"] / max_cost)
    h_wob = 0.05 + 0.9 * (wob["mean_liar_cost_usdc"] / max_cost)

    W, H = 640, 360
    left, top = 130, 60
    plot_w, plot_h = 380, 220
    baseline = top + plot_h

    def bar(x, w, hfrac, color, label, val):
        bh = int(hfrac * plot_h)
        y = baseline - bh
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{bh}" fill="{color}" '
                f'rx="6"/>'
                f'<text x="{x + w/2}" y="{y - 12}" text-anchor="middle" '
                f'font-family="monospace" font-size="16" fill="#e6edf3">'
                f'${val/1e6:.2f}</text>'
                f'<text x="{x + w/2}" y="{baseline + 26}" text-anchor="middle" '
                f'font-family="monospace" font-size="15" fill="#8b949e">'
                f'{label}</text>')

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <rect width="{W}" height="{H}" fill="#000410"/>
  <text x="30" y="40" font-family="monospace" font-size="22" fill="#00d4ff" font-weight="bold">Proof-of-Memory — cost to lie</text>
  <text x="30" y="64" font-family="monospace" font-size="13" fill="#8b949e">mean liar cost per false claim (N={result['n']}, seed={result['seed']})</text>
  <line x1="{left}" y1="{baseline}" x2="{left + plot_w}" y2="{baseline}" stroke="#30363d" stroke-width="2"/>
  <text x="20" y="{baseline - plot_h/2}" text-anchor="middle" font-family="monospace" font-size="13" fill="#8b949e" transform="rotate(-90 20 {baseline - plot_h/2})">USDC lost by liar</text>
  {bar(left + 40, 120, h_wb, '#00d4ff', 'with bond', wb['mean_liar_cost_usdc'])}
  {bar(left + 220, 120, h_wob, '#f85149', 'without bond', wob['mean_liar_cost_usdc'])}
  <text x="{left + 40}" y="{H - 22}" font-family="monospace" font-size="13" fill="#8b949e">{wb['falsified']}/{wb['n']} lies slashed · challenger nets ${wb['total_slashed_usdc']/1e6:.2f}</text>
  <text x="{left + 220}" y="{H - 22}" font-family="monospace" font-size="13" fill="#8b949e">{wob['survived']}/{wob['n']} cost the liar nothing</text>
</svg>"""
    path.write_text(svg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--stake-usdc", type=int, default=100_000)
    ap.add_argument("--counter-usdc", type=int, default=20_000)
    args = ap.parse_args()

    result = run_ablation(args.n, args.seed, args.stake_usdc, args.counter_usdc)
    wb, wob = result["with_bond"], result["without_bond"]

    print("=" * 68)
    print("PROOF-OF-MEMORY — COST TO LIE (honest ablation)")
    print(f"N={result['n']}  seed={result['seed']}  "
          f"stake=${result['stake_usdc']/1e6:.2f}  "
          f"counter=${result['counter_usdc']/1e6:.2f}")
    print("=" * 68)
    print(f"  WITH bond    — liar cost ${wb['mean_liar_cost_usdc']/1e6:.2f} "
          f"({wb['falsified']}/{wb['n']} slashed) · challenger nets "
          f"${wb['total_slashed_usdc']/1e6:.2f}")
    print(f"  WITHOUT bond — liar cost ${wob['mean_liar_cost_usdc']/1e6:.2f} "
          f"({wob['n']}/{wob['n']} free)")
    print("-" * 68)
    if wob["mean_liar_cost_usdc"] == 0:
        print(f"  HEADLINE: lying costs ${wb['mean_liar_cost_usdc']/1e6:.2f}/claim "
              f"with a bond vs $0.00 without — bonds make false claims "
              f"net-negative, memory you'd bet on.")
    else:
        cost_ratio = wb["mean_liar_cost_usdc"] / wob["mean_liar_cost_usdc"]
        print(f"  HEADLINE: lying is {cost_ratio:,.0f}x more expensive with a bond.")
    print("=" * 68)

    out_json = REPO / "data" / "bond_economics.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, indent=2))
    print(f"  wrote {out_json.relative_to(REPO)}")

    svg_path = REPO / "docs" / "assets" / "bond_economics.svg"
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    render_svg(result, svg_path)
    print(f"  wrote {svg_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
