"""
devig.py -- take a two-way price pair apart into the market's fair chance. (the maths behind the "EV dingers" process)

A two-way market (over/under, yes/no) has two prices whose implied chances add to MORE than 100%; the extra is the cut.
Four standard ways to remove it, from simplest to most careful about longshots:

  multiplicative   divide each chance by the total. Treats the cut as proportional. Slightly OVER-rates longshots.
  additive         subtract an equal slice of the cut from each side.
  power            raise each chance to a power k so they sum to 1.
  probit           work in z-scores: shift both by one constant c so Phi(z1 - c) + Phi(z2 - c) = 1.
                   Puts a bit less of the cut on the favourite and more on the longshot -- the right direction, because the
                   book's cut sits mostly on the long price. This is the method behind "+7% EV probit".

Check against the worked example in the process notes: Kalshi +400 / -426 -> probit fair 19.5%; a +450 price is then
0.195 x 5.5 - 1 = +7.4% EV. Circa +365 / -470 -> fair 19.5%, +7.1%. (tests: python bots/devig.py)
"""
from statistics import NormalDist
from typing import Dict, Tuple

_N = NormalDist()


def implied(american: float) -> float:
    a = float(american)
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        raise ValueError("p must be inside (0,1)")
    return round(100 * (1 - p) / p) if p < 0.5 else -round(100 * p / (1 - p))


def multiplicative(p1: float, p2: float) -> Tuple[float, float]:
    s = p1 + p2
    return p1 / s, p2 / s


def additive(p1: float, p2: float) -> Tuple[float, float]:
    cut = (p1 + p2 - 1) / 2
    return p1 - cut, p2 - cut


def power(p1: float, p2: float) -> Tuple[float, float]:
    lo, hi = 0.5, 3.0
    for _ in range(80):
        k = (lo + hi) / 2
        if p1 ** k + p2 ** k > 1:
            lo = k
        else:
            hi = k
    return p1 ** k, p2 ** k


def probit(p1: float, p2: float) -> Tuple[float, float]:
    z1, z2 = _N.inv_cdf(p1), _N.inv_cdf(p2)
    lo, hi = -2.0, 2.0
    for _ in range(80):
        c = (lo + hi) / 2
        if _N.cdf(z1 - c) + _N.cdf(z2 - c) > 1:
            lo = c
        else:
            hi = c
    return _N.cdf(z1 - c), _N.cdf(z2 - c)


METHODS = {"multiplicative": multiplicative, "additive": additive, "power": power, "probit": probit}


def fair(price_a: float, price_b: float, method: str = "probit") -> Tuple[float, float]:
    """(fair chance of A, fair chance of B) from the two American prices."""
    return METHODS[method](implied(price_a), implied(price_b))


def ev(fair_p: float, american: float) -> float:
    """expected profit per $1 staked at this American price if the true chance is fair_p."""
    a = float(american)
    pay = a / 100 if a > 0 else 100 / -a
    return fair_p * pay - (1 - fair_p)


def fair_table(price_a: float, price_b: float, target_price: float) -> Dict[str, Dict[str, float]]:
    out = {}
    for m in METHODS:
        fa, _ = fair(price_a, price_b, m)
        out[m] = {"fair_pct": round(100 * fa, 2), "ev_pct": round(100 * ev(fa, target_price), 2)}
    return out


if __name__ == "__main__":
    k = fair_table(400, -426, 450)
    c = fair_table(365, -470, 450)
    assert abs(k["probit"]["fair_pct"] - 19.5) < 0.1 and abs(k["probit"]["ev_pct"] - 7.4) < 0.3, k
    assert abs(c["probit"]["ev_pct"] - 7.1) < 0.3, c
    assert k["multiplicative"]["fair_pct"] > k["probit"]["fair_pct"], "multiplicative over-rates the longshot"
    assert abs(sum(fair(400, -426)) - 1) < 1e-9
    print("Kalshi +400/-426 vs a +450 price:", k)
    print("Circa  +365/-470 vs a +450 price:", c)
    print("devig.py tests pass")
