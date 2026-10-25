#!/usr/bin/env python3
"""
staking.py -- how much to risk on an edge, built to be careful with a model that has not yet proved itself.

    python bots/staking.py --p 0.52 --price +120 [--n 300] [--bankroll 1000] [--kelly-frac 0.25] [--cap 0.01]
    python bots/staking.py --table                                  a quick grid: what the rule stakes at different edges and sample sizes

The rule (every step is there because the model's probability is an ESTIMATE, and the biggest bets come from its biggest errors):
  1. SHRINK our probability toward the price. p_eff = implied + w * (p_model - implied), w = n / (n + N0), n = graded bets in this market with the model's edge on, N0 = 500.
     With no track record (n = 0) w = 0 and the stake is zero; at n = 500 the model is trusted halfway; at 2,000 about 80%.
  2. KELLY on the shrunk probability: f = (b*p_eff - (1 - p_eff)) / b, b = the decimal odds minus one.
  3. FRACTIONAL: take 25% of Kelly (a full Kelly bankroll swings too hard when the edge is uncertain).
  4. CAP: never more than 1% of the bankroll on one bet; correlated bets (same game, same player) count together.
  5. FEES: on Kalshi, subtract the taker fee 0.07 * p * (1 - p) from the edge first.
It does not place anything. It is the number the paper log (live_edge.py) records beside each alert, and the one to use by hand once the reports show a lane has earned it.
"""
import argparse

N0 = 500.0


def implied(price):
    return 100.0 / (price + 100.0) if price > 0 else -price / (-price + 100.0)


def decimal_profit(price):
    return price / 100.0 if price > 0 else 100.0 / -price


def stake_fraction(p_model, price, n=0, kelly_frac=0.25, cap=0.01, fee=0.0):
    """-> (stake as a fraction of bankroll, shrunk probability, trust weight). price = American odds; fee = per-contract fee in probability units (Kalshi)."""
    imp = implied(price) + fee
    w = n / (n + N0)
    p_eff = imp + w * (p_model - imp)
    b = decimal_profit(price) * (1 - fee / max(1e-9, 1.0)) if fee else decimal_profit(price)
    f = (b * p_eff - (1 - p_eff)) / b
    return max(0.0, min(cap, kelly_frac * f)), p_eff, w


def kalshi_fee(p):
    return 0.07 * p * (1 - p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--p", type=float); ap.add_argument("--price", type=int); ap.add_argument("--n", type=int, default=0); ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--kelly-frac", type=float, default=0.25); ap.add_argument("--cap", type=float, default=0.01); ap.add_argument("--table", action="store_true"); a = ap.parse_args()
    if a.table:
        print("stake as % of bankroll (25% Kelly on the shrunk probability, 1% cap), price +100 (even money)")
        print("  edge (model - price):   " + "".join(f"{n:>10}" for n in ("n=0", "n=100", "n=500", "n=1000", "n=3000")))
        for e in (0.02, 0.04, 0.06, 0.10):
            print(f"  +{100 * e:>3.0f} pts                " + "".join(f"{100 * stake_fraction(0.5 + e, 100, n)[0]:>9.2f}%" for n in (0, 100, 500, 1000, 3000)))
    else:
        f, pe, w = stake_fraction(a.p, a.price, a.n, a.kelly_frac, a.cap)
        print(f"model {100 * a.p:.1f}% vs price implied {100 * implied(a.price):.1f}%  ->  trust {100 * w:.0f}% (n={a.n}), shrunk probability {100 * pe:.1f}%  ->  stake {100 * f:.2f}% of bankroll = ${f * a.bankroll:.2f} of ${a.bankroll:.0f}")
