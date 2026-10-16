"""model_harness: the maths and the promotion rule. TEST data below, labelled; nothing here is a real result.
Run: PYTHONPATH=. python3 tests/test_model_harness.py"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import model_harness as H

# wilson: 0 of 0 is nothing; 50 of 100 is centred near 50
assert H.wilson(0, 0) == (0.0, 0.0)
lo, hi = H.wilson(50, 100)
assert 39 < lo < 41 and 59 < hi < 61, (lo, hi)

# paired: only games where the two picks differ carry information
p = H.paired([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
assert (p["only_a"], p["only_b"], p["games"]) == (1, 1, 5) and p["z"] == 0.0
p = H.paired([1] * 30 + [0] * 70, [0] * 100)
assert p["diff_pts"] == 30.0 and p["z"] > 5

def rows(n_games, picks):
    """TEST rows: each game has 3 players; picks[score] = function(game_index) -> index of the player that score picks; y set by 'who homers'."""
    out = []
    for g in range(n_games):
        for i in range(3):
            out.append({"night": f"2099-01-{1 + g % 28:02d}", "game": f"g{g}", "id": f"p{g}-{i}", "y": 1 if i == picks["truth"](g) else 0,
                        "s": {k: (10 if picks[k](g) == i else 1) for k in ("prod", "good", "base")}})
    return out

# a candidate that is right far more often than production AND the baseline is promoted; with too few games it is held
picks = {"truth": lambda g: g % 3, "prod": lambda g: (g + 1) % 3, "good": lambda g: g % 3, "base": lambda g: (g + 2) % 3}
big = H.evaluate(rows(300, picks), ["prod", "good", "base"], "prod", "base")
assert {r["score"]: r["verdict"] for r in big["scores"]}["good"].startswith("BEATS production and the baseline"), big["scores"][1]["verdict"]
small = H.evaluate(rows(60, picks), ["prod", "good", "base"], "prod", "base")
assert "not enough games" in {r["score"]: r["verdict"] for r in small["scores"]}["good"]
# determinism: same rows, same answer
assert H.evaluate(rows(300, picks), ["prod", "good", "base"], "prod", "base") == big
# a candidate that beats production but not the baseline is not promoted
picks2 = {"truth": lambda g: g % 3, "prod": lambda g: (g + 1) % 3, "good": lambda g: g % 3, "base": lambda g: g % 3}
held = H.evaluate(rows(300, picks2), ["prod", "good", "base"], "prod", "base")
assert "hold" in {r["score"]: r["verdict"] for r in held["scores"]}["good"] or "no clear" in {r["score"]: r["verdict"] for r in held["scores"]}["good"]
print("ok   model_harness: wilson, paired test, promotion rule (needs production AND baseline), determinism (TEST data)")
