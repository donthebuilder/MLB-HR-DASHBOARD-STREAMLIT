"""Postponed / cancelled games leave the slate; others stay (2026-10-04).
Run: PYTHONPATH=. python3 tests/test_playable_games.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bots"))
import mlb_dashboard as m  # noqa: E402
# real statsapi /gameStatus values: (detailedState, statusCode, codedGameState)
g = lambda pk, d, sc, cg: {"gamePk": pk, "status": {"detailedState": d, "statusCode": sc, "codedGameState": cg}}
games = [g(1, "Scheduled", "S", "S"), g(2, "Postponed: Rain", "DR", "D"), g(3, "Cancelled", "CO", "C"),
         g(4, "Suspended: Rain", "TR", "T"), g(5, "Pre-Game", "P", "P"), g(6, "Warmup", "PW", "P"),
         g(7, "", "DO", "D"), g(8, "Delayed: Rain", "IR", "I")]
kept = [x["gamePk"] for x in m.playable_games(games)]
ok = kept == [1, 4, 5, 6, 8]
print("kept", kept, "OK" if ok else "FAIL (want [1, 4, 5, 6, 8])")
sys.exit(0 if ok else 1)
