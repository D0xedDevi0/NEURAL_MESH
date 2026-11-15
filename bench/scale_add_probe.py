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
# Above this much hypervisor steal, the run is measuring the host, not the code.
STEAL_LIMIT_PCT = 15.0


def cpu_counters():
    """(total, steal) jiffies from /proc/stat — steal is the 9th field.

    Steal is CPU the hypervisor took away from this VM. It is the single biggest
    source of error in every timing on this host and it is invisible to our own
    process list: local usage has been measured at ~5% of one core while steal sat
    at 51-64%, so a slow measurement here usually says nothing about the code.
    """
    with open("/proc/stat") as fh:
        parts = [float(x) for x in fh.readline().split()[1:]]
    total = sum(parts)
    steal = parts[7] if len(parts) > 7 else 0.0
    return total, steal


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
        c0 = cpu_counters()
        for _ in range(REPS):
            t0 = time.perf_counter()
            mesh.add(f"fact {n} about topic {n % 17} and lane {n % 5}")
            samples.append(time.perf_counter() - t0)
            n += 1
        c1 = cpu_counters()
        d_total, d_steal = c1[0] - c0[0], c1[1] - c0[1]
        steal_pct = round(100.0 * d_steal / d_total, 1) if d_total > 0 else 0.0
        mean = sum(samples) / len(samples)
        points.append({"n": n, "mean_add_s": round(mean, 4), "steal_pct": steal_pct,
                       "min_s": round(min(samples), 4), "max_s": round(max(samples), 4),
                       "loadavg": round(os.getloadavg()[0], 2)})
        print(f"n={n:>5}  add {mean * 1000:>9.1f} ms   "
              f"(min {min(samples) * 1000:.1f}, max {max(samples) * 1000:.1f})  "
              f"steal {steal_pct:>4.1f}%", flush=True)

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
    if len(tail) >= 2:
        txs = [math.log(p["n"]) for p in tail]
        tys = [math.log(p["mean_add_s"]) for p in tail]
        tmx, tmy = sum(txs) / len(txs), sum(tys) / len(tys)
        tden = sum((x - tmx) ** 2 for x in txs)
        tail_slope = (sum((x - tmx) * (y - tmy) for x, y in zip(txs, tys)) / tden
                      if tden else float("nan"))
        tail_pred = math.exp(tmy - tail_slope * tmx) * (LIVE_SCALE ** tail_slope)
    else:
        # a one- or two-point ladder has no tail to fit; say so instead of dividing by
        # zero, which is how this crashed the first time it was run with a short ladder
        tail_slope = tail_pred = float("nan")

    # sanity: how well does the fit describe the points it came from?
    worst = max(abs(math.exp(intercept) * (p["n"] ** slope) - p["mean_add_s"]) /
                p["mean_add_s"] for p in points)

    out = {
        "sizes": SIZES, "reps": REPS, "points": points,
        "fitted_exponent": round(slope, 3) if slope == slope else None,
        "interpretation": ("n/a — too few points to fit" if slope != slope else
                           "linear" if slope < 1.35 else
                           "super-linear, sub-quadratic" if slope < 1.8 else
                           "quadratic or worse"),
        # NaN is not valid JSON (json.dump writes it anyway and strict readers reject
        # the file), so anything uncomputable is null
        "worst_fit_error_pct": round(worst * 100, 1) if worst == worst else None,
        "predicted_add_at_2500_s": round(predict, 2) if predict == predict else None,
        "tail_exponent_excluding_smallest": (round(tail_slope, 3)
                                             if tail_slope == tail_slope else None),
        "tail_predicted_add_at_2500_s": (round(tail_pred, 2)
                                         if tail_pred == tail_pred else None),
        "loadavg_at_end": round(os.getloadavg()[0], 2),
        "wall_seconds": round(time.perf_counter() - t_start, 1),
    }
    worst_steal = max(p["steal_pct"] for p in points)
    trusted = worst_steal <= STEAL_LIMIT_PCT
    out["worst_steal_pct"] = worst_steal
    out["trustworthy"] = trusted
    out["steal_limit_pct"] = STEAL_LIMIT_PCT

    print("\n" + json.dumps(out, indent=1))
    if slope == slope:
        tail_label = (f"tail exponent (n>={points[1]['n']}) {tail_slope:.2f} -> "
                      f"{tail_pred:.1f} s/add" if tail_slope == tail_slope
                      else "tail fit n/a (needs >=3 points)")
        print(f"\nVERDICT: all-points exponent {slope:.2f} -> {predict:.1f} s/add at "
              f"n={LIVE_SCALE}; {tail_label}. Load at end {out['loadavg_at_end']}.")
    else:
        print(f"\nVERDICT: too few points to fit an exponent "
              f"({len(points)} point(s)). Load at end {out['loadavg_at_end']}.")
    print(f"\nsteal check: worst point {worst_steal:.1f}% (limit {STEAL_LIMIT_PCT}%)")
    if not trusted:
        print("DEGRADED: the hypervisor took this much CPU away from the VM during the "
              "run, so these times describe the host's mood, not Mesh.add. Refusing to "
              "publish the curve — rerun when steal is low (watch `top` for %st).")
        return 3

    with open("/opt/data/cache/scratch/scale_add.json", "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
