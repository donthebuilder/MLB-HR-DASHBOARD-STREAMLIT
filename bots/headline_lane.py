"""
headline_lane.py -- the ONE lane a player card should lead with, shared by every sport's board.

A card used to lead with a fixed lane (NBA: 25+ points) even when the player stands out somewhere else (a three-point shooter). The headline lane is the lane
where the model has him furthest above his own season rate (p / lane_base), among lanes that are not a long shot. The site's CALLED label still wins when the
player is called in a lane: the site passes that lane in and shows it instead; this is the default for everyone else.
"""

MIN_P = 0.08            # a lane under 8% is never the headline (a 1% lane at 3x his rate is still a 3% lane)
MIN_BASE = 0.02         # and his own rate must exist and not be near zero (a lift off a tiny base is noise)

LABELS = {
    "nba": {"reb": "Rebounds 10+", "ast": "Assists 8+", "3pm": "3PM 4+", "pra": "PRA 35+", "dd": "Double-double"},
    "nhl": {"pts": "Points", "ast": "Assists", "sog": "Shots 3+"},
    "mlb": {"hit": "Hit", "hrr": "H+R+RBI", "tb2": "2+ bases", "hr": "Home run"},
}


def headline_lane(probs, base, sport):
    """{lane, label, p, base, lift} for the best lane in `probs` ({lane: p}) against `base` ({lane: his season rate}); None if no lane qualifies."""
    best = None
    for lane, p in (probs or {}).items():
        b = (base or {}).get(lane)
        if p is None or b is None or p < MIN_P or b < MIN_BASE:
            continue
        lift = p / b
        if best is None or lift > best["lift"]:
            best = {"lane": lane, "label": LABELS.get(sport, {}).get(lane, lane), "p": round(p, 4), "base": round(b, 4), "lift": round(lift, 2)}
    return best
