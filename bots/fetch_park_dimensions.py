#!/usr/bin/env python3
"""fetch_park_dimensions.py -- real wall HEIGHTS, not just distances.

    python bots/fetch_park_dimensions.py --dry-run
    python bots/fetch_park_dimensions.py --out ../moonshot-push/lib/parkWalls.js

WHY THIS EXISTS. lib/walls.js on the site pulls statsapi's
`venues?hydrate=fieldInfo`, which carries leftLine/leftCenter/center/
rightCenter/rightLine -- distances, and NOTHING about wall height. That is not
a bug in that file; the payload genuinely does not have it. So the spray
chart's "would this have cleared the fence here" question was answerable for
distance and not for height, which is exactly backwards for Fenway (a fly ball
past the 310 line in left is still a double off a 37-ft Monster) or Chase
Field's 24-ft-plus batter's eye in center.

Baseball Savant's own Park Factors page, filtered to `type=dimensions`, is the
public source that has both: distance AND height at the same five points
(LF line / LF-CF gap / CF / CF-RF gap / RF line). It is not a JSON API --
the numbers are server-rendered straight into the page as `var data = [...]`
-- so this fetches the HTML and pulls that array out with a regex rather than
reverse-engineering a private endpoint.

TWO TRAPS, FOUND BY ACTUALLY PULLING THE DATA (2026-08-29 audit + this run):

1. Savant tags a small number of rows `is_diff_configuration: 1` where a park
   has more than one recorded configuration. Kauffman Stadium's ONLY row for a
   given season carries that flag -- so a fetcher that filters those rows out
   as "not the primary configuration" silently loses the Royals' park
   entirely. This script keeps every row Savant returns; it does not filter
   on that flag.
2. Heights are not static across seasons -- Savant itself flags certain years
   (2022, 2025) as wall-change years. Re-run this per season. Don't assume a
   file generated in April is still right in August of a different year.

WHAT THIS DOES NOT DO. It does not touch the live site or any bot pipeline --
it writes one static JS file (lib/parkWalls.js in the moonshot-push repo) that
SprayField.js imports directly. Re-run it and copy the output over by hand (or
point --out at a sibling checkout) whenever Savant's numbers move.
"""
import argparse
import json
import re
import sys
import time
import urllib.request

SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/statcast-park-factors?type=dimensions&year={year}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Same-park name aliases the bot's own `venue_name` field is known to use at
# different times (sponsor renames). Duplicated under both keys rather than
# resolved through a second normalize step at read time -- matches how the
# file this script generates has always been keyed (by the literal string the
# bot publishes), so SprayField's `PARKS[venue]` lookup stays a flat dict hit.
ALIASES = {
    "Dodger Stadium": ["UNIQLO Field at Dodger Stadium"],
    "Rate Field": ["Guaranteed Rate Field"],
}

# Not a home park for any of the 30 clubs, but Savant carries a row for them
# and the bot occasionally scores a game played there.
SPECIAL_VENUES = {"Field of Dreams", "Journey Bank Ballpark"}


def fetch_raw(year: int, retries: int = 3) -> str:
    url = SAVANT_URL.format(year=year)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 -- surfaced to the caller below
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url} after {retries} tries: {last_err}")


def extract_rows(html: str) -> list[dict]:
    # The page embeds several `var xxx = [...]` blocks; the one we want is the
    # one whose objects carry height_lf_line (present only on the dimensions
    # view). Scan every `var NAME = [...]` and pick the match, rather than
    # assuming the variable is always literally named `data` -- Savant has
    # renamed this before between page revisions.
    for m in re.finditer(r"var\s+\w+\s*=\s*(\[[\s\S]*?\]);", html):
        blob = m.group(1)
        if "height_lf_line" in blob or "height_cf" in blob:
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(
        "no `var ... = [...]` block containing height_lf_line/height_cf found -- "
        "Savant likely changed the page. Inspect the HTML by hand before retrying."
    )


def round1(x):
    return round(float(x), 2) if x is not None else None


def build_park_walls(rows: list[dict]) -> dict:
    out = {}
    seen_teams = set()
    for v in rows:
        name = v.get("venue_name_short")
        if not name:
            continue
        d = [
            round1(v.get("distance_lf_line")), round1(v.get("distance_lf_gap")),
            round1(v.get("distance_cf")), round1(v.get("distance_rf_gap")),
            round1(v.get("distance_rf_line")),
        ]
        h = [
            round1(v.get("height_lf_line")), round1(v.get("height_lf_gap")),
            round1(v.get("height_cf")), round1(v.get("height_rf_gap")),
            round1(v.get("height_rf_line")),
        ]
        if any(x is None for x in d) or any(x is None for x in h):
            # Missing a field entirely (rather than a genuine 0) means this
            # row isn't a real dimensions row -- skip it instead of writing a
            # park with a null wall in the middle of the outfield.
            print(f"  skip {name}: incomplete row", file=sys.stderr)
            continue
        out[name] = {"d": d, "h": h}
        team = v.get("name_display_club")
        if team:
            seen_teams.add(team)
        for alias in ALIASES.get(name, []):
            out[alias] = {"d": d, "h": h}

    missing_special = SPECIAL_VENUES - set(out)
    if missing_special:
        print(f"  note: Savant had no row this year for {sorted(missing_special)} "
              "-- carrying forward whatever the site file already has for them, "
              "if anything.", file=sys.stderr)
    return out


def render_js(park_walls: dict, year: int) -> str:
    lines = []
    lines.append("// \U0001f9f1 Real MLB park dimensions AND wall heights, one source.")
    lines.append("//")
    lines.append(f"// GENERATED by bots/fetch_park_dimensions.py against Savant's {year} dimensions")
    lines.append("// leaderboard. Do not hand-edit -- re-run the script and regenerate instead.")
    lines.append("// statsapi's `venues?hydrate=fieldInfo` (lib/walls.js) gives distance only; this")
    lines.append("// is the only public source with wall HEIGHT at the same five points.")
    lines.append("//")
    lines.append("// Format: d = [LF line, LF-CF gap, CF, CF-RF gap, RF line], feet.")
    lines.append("//         h = wall height at those same five points, feet.")
    lines.append("//")
    lines.append("// Heights shift between seasons (Savant flags 2022 and 2025 as wall-change")
    lines.append("// years) -- re-run this per season, don't freeze it across years.")
    lines.append("")
    lines.append("export const PARK_WALLS = {")
    name_w = max(len(n) for n in park_walls) + 3
    for name in sorted(park_walls):
        rec = park_walls[name]
        key = f"'{name}':".ljust(name_w)
        d = ", ".join(f"{x:.1f}" for x in rec["d"])
        h = ", ".join(f"{x:.2f}" for x in rec["h"])
        lines.append(f"  {key} {{ d: [{d}], h: [{h}] }},")
    lines.append("}")
    lines.append("")
    lines.append("export default PARK_WALLS")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--out", default=None, help="path to write lib/parkWalls.js (default: print to stdout)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + parse only, print a summary, write nothing")
    args = ap.parse_args()

    print(f"fetching Savant park-factors dimensions for {args.year}...", file=sys.stderr)
    html = fetch_raw(args.year)
    rows = extract_rows(html)
    print(f"  {len(rows)} venue rows found", file=sys.stderr)

    park_walls = build_park_walls(rows)
    print(f"  {len(park_walls)} venues kept (aliases included)", file=sys.stderr)

    if args.dry_run:
        for name in sorted(park_walls):
            print(f"  {name}: d={park_walls[name]['d']} h={park_walls[name]['h']}")
        print("dry run -- nothing written", file=sys.stderr)
        return

    js = render_js(park_walls, args.year)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(js)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(js)


if __name__ == "__main__":
    main()
