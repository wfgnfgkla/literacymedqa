#!/usr/bin/env python3
"""
Sample size for the literacy gap, under McNemar's test.

The headline comparison is paired: the same model answers the same item at
level (a) doctor wording and level (c) low-literacy wording. Only discordant
items -- right at one level, wrong at the other -- carry information, so N is
driven by the discordance rate, not by the accuracy levels themselves.

Notation
    N      items in the frozen set
    pi_d   proportion of items that are discordant (flip in either direction)
    gap    net accuracy drop, acc(a) - acc(c), i.e. (c_10 - c_01) / N
    psi    share of discordant items that flip right->wrong  = (pi_d + gap) / (2*pi_d)

Normal approximation for the number of discordant pairs required:
    n_d = (z_alpha/2 + z_beta)^2 / (4 * (psi - 0.5)^2)
    N   = n_d / pi_d

Usage
    python src/power.py                       # default table
    python src/power.py --gap 0.05 --pi-d 0.15
"""
from __future__ import annotations

import argparse
import math


def _z(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def required_n(gap: float, pi_d: float, alpha: float = 0.05, power: float = 0.80) -> dict:
    """Items needed to detect a net accuracy gap at the given discordance rate."""
    if not 0 < gap < pi_d < 1:
        raise ValueError("need 0 < gap < pi_d < 1: the net gap cannot exceed total discordance")
    psi = (pi_d + gap) / (2 * pi_d)
    za, zb = _z(1 - alpha / 2), _z(power)
    n_d = (za + zb) ** 2 / (4 * (psi - 0.5) ** 2)
    n = n_d / pi_d
    return {
        "gap": gap, "pi_d": pi_d, "psi": round(psi, 4), "alpha": alpha, "power": power,
        "discordant_pairs_needed": math.ceil(n_d), "items_needed": math.ceil(n),
    }


POOL = 1273  # MedQA US 4-option test split


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, help="net accuracy drop to detect, e.g. 0.05")
    ap.add_argument("--pi-d", type=float, help="expected discordance rate, e.g. 0.15")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--power", type=float, default=0.80)
    args = ap.parse_args()

    if args.gap and args.pi_d:
        r = required_n(args.gap, args.pi_d, args.alpha, args.power)
        print(r)
        return

    print(f"Items needed per model, McNemar, alpha={args.alpha}, power={args.power}")
    print(f"(test-split pool = {POOL}; '--' means it exceeds the pool)\n")
    pids = [0.10, 0.15, 0.20, 0.25]
    print("gap    " + "".join(f"pi_d={p:<9.2f}" for p in pids))
    for gap in (0.03, 0.05, 0.07, 0.10, 0.15):
        row = f"{gap:<7.2f}"
        for pid in pids:
            if gap >= pid:
                row += f"{'n/a':<14}"
                continue
            n = required_n(gap, pid, args.alpha, args.power)["items_needed"]
            row += f"{(str(n) if n <= POOL else f'{n} --'):<14}"
        print(row)
    print("\nRead: a 5-point gap with 15% of items flipping needs ~"
          f"{required_n(0.05, 0.15)['items_needed']} items.")
    print("Pick pi_d from the pilot, not from this table. If the pilot shows a large")
    print("gap you are over-powered at N=500; if it shows a small one, no feasible N")
    print("inside a 1,273-item pool will rescue it and the claim has to soften.")


if __name__ == "__main__":
    main()
