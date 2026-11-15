#!/usr/bin/env python3
"""Does Mesh.add actually go quadratic? How bad does it get at the live scale?

The docs assert O(n^2) auto-linking and at n=120 an add already costs ~200 ms. If that
holds, a 2.5k-node store costs roughly 90 s per add, which decides whether a memory
provider can be installed at all. Measure the curve, fit the exponent, extrapolate.

Times adds at a ladder of store sizes, then fits log(t) = a + b*log(n). b is the
growth exponent: ~1.0 is linear, ~2.0 is quadratic. Extrapolation is only as good as
that fit, so the fitted b and the points are both printed rather than just the answer.
"""
import json
import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from neural_mesh import Mesh  # noqa: E402

SIZES = [25, 50, 100, 200, 400]
REPS = 5
LIVE_SCALE = 2500


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="scale-add-")
    mesh = Mesh(os.path.join(tmp, "s.db"), validator=False)

    n = 0
    points = []
    t_start = time.perf_counter()
    for size in SIZES:
        # fill up to the size (untimed — this IS the cost being measured, just at
        # smaller n, so it can't be skipped, only excluded from the per-size sample)
        while n < size:
            mesh.add(f"fact {n} about topic {n % 17} and lane {n % 5}")
            n += 1
        samples = []
        for _ in range(REPS):
            t0 = time.perf_counter()
            mesh.add(f"fact {n} about topic {n % 17} and lane {n % 5}")
            samples.append(time.perf_counter() - t0)
            n += 1
        mean = sum(samples) / len(samples)
        points.append({"n": n, "mean_add_s": round(mean, 4),
                       "min_s": round(min(samples), 4), "max_s": round(max(samples), 4),
                       "loadavg": round(os.getloadavg()[0], 2)})
        print(f"n={n:>5}  add {mean * 1000:>9.1f} ms   "
              f"(min {min(samples) * 1000:.1f}, max {max(samples) * 1000:.1f})", flush=True)

    # least-squares fit of log(t) vs log(n)
    xs = [math.log(p["n"]) for p in points]
    ys = [math.log(p["mean_add_s"]) for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else float("nan")
    intercept = my - slope * mx
    predict = math.exp(intercept) * (LIVE_SCALE ** slope)

    # The smallest size is often off-trend: a nearly empty store has no links to
    # compare against, so it understates the cost at every larger n. Report the tail
    # fit as well, or one cold point sets the exponent for the whole curve.
    tail = points[1:]
    txs = [math.log(p["n"]) for p in tail]
    tys = [math.log(p["mean_add_s"]) for p in tail]
    tmx, tmy = sum(txs) / len(txs), sum(tys) / len(tys)
    tden = sum((x - tmx) ** 2 for x in txs)
    tail_slope = (sum((x - tmx) * (y - tmy) for x, y in zip(txs, tys)) / tden
                  if tden else float("nan"))
    tail_pred = math.exp(tmy - tail_slope * tmx) * (LIVE_SCALE ** tail_slope)

    # sanity: how well does the fit describe the points it came from?
    worst = max(abs(math.exp(intercept) * (p["n"] ** slope) - p["mean_add_s"]) /
                p["mean_add_s"] for p in points)

    out = {
        "sizes": SIZES, "reps": REPS, "points": points,
        "fitted_exponent": round(slope, 3),
        "interpretation": ("linear" if slope < 1.35 else
                           "super-linear, sub-quadratic" if slope < 1.8 else
                           "quadratic or worse"),
        "worst_fit_error_pct": round(worst * 100, 1),
        "predicted_add_at_2500_s": round(predict, 2),
        "tail_exponent_excluding_smallest": round(tail_slope, 3),
        "tail_predicted_add_at_2500_s": round(tail_pred, 2),
        "loadavg_at_end": round(os.getloadavg()[0], 2),
        "wall_seconds": round(time.perf_counter() - t_start, 1),
    }
    print("\n" + json.dumps(out, indent=1))
    print(f"\nVERDICT: all-points exponent {slope:.2f} -> {predict:.1f} s/add at "
          f"n={LIVE_SCALE}; tail exponent (n>={points[1]['n']}) {tail_slope:.2f} -> "
          f"{tail_pred:.1f} s/add. Load at end {out['loadavg_at_end']}.")
    with open("/opt/data/cache/scratch/scale_add.json", "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
