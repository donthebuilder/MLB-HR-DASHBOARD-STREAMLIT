#!/usr/bin/env python3
"""
🔍 SHAPE GUARD — .get() on a payload that might be a bare list.

2026-08-10. This exact bug has now taken the pipeline down twice:

  · 2026-08-09  four accountability scripts each did payload.get() on graded
                files, ten of which are bare top-level lists. Fixed by adding
                rows_of() in bots/archive.py and routing all four through it.
  · 2026-08-10  bots/pick_lock.py did the SAME THING on the slate file and
                killed the whole Today workflow with
                `AttributeError: 'list' object has no attribute 'get'`.

The second one is the interesting failure, and it is mine. I knew the class,
fixed four instances, and never grepped for a fifth. Patching instances of a
known class is how you get to do the work twice.

So this greps for the class. It finds every variable assigned from json.load /
json.loads and flags any unguarded `.get(` on it. "Guarded" means an isinstance
check or a rows_of/meta_of call within a few lines.

It will have false positives — files whose shape our own writer fixes are fine
in practice. The point is that adding one costs a two-line guard and missing
one costs a night's slate, so the trade is not close.

    python3 bots/check_shapes.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Files whose payload shape is guaranteed by our own writer, checked by hand.
# Anything NOT on this list has to guard.
ALLOW = {
    # (file, line-substring) pairs deliberately exempted
}


def main() -> int:
    hits = []
    for f in sorted(HERE.glob("*.py")):
        if f.name in ("check_shapes.py", "archive.py"):
            continue
        lines = f.read_text().split("\n")
        loaded = set()
        for line in lines:
            m = re.search(r"^\s*(\w+)\s*=\s*json\.loads?\(", line)
            if m:
                loaded.add(m.group(1))
        for i, line in enumerate(lines):
            for v in loaded:
                if not re.search(rf"(?<![\w.]){re.escape(v)}\.get\(", line):
                    continue
                # 40 lines, not 5. The first version missed guards that sat at the
                # top of a function while the .get() was further down its body,
                # and reported four false positives that were already safe.
                ctx = "\n".join(lines[max(0, i - 40):i + 1])
                guarded = (
                    f"isinstance({v}" in ctx
                    or "rows_of(" in ctx
                    or "meta_of(" in ctx
                    or re.search(rf"{re.escape(v)}\s*if\s+isinstance", ctx)
                )
                if not guarded:
                    hits.append((f.name, i + 1, line.strip()[:88]))

    if hits:
        print("Unguarded .get() on a json-loaded payload — each one is a")
        print("potential 'list object has no attribute get' at 3am:\n")
        for name, n, line in hits:
            print(f"  {name}:{n}\n      {line}")
        print(f"\n{len(hits)} to guard. Use `isinstance(x, dict)` or "
              f"archive.meta_of()/rows_of().")
        return 1
    print("ok   no unguarded .get() on a json-loaded payload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
