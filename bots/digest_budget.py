"""Fit a digest's sections into Discord embeds without losing a line quietly.

An embed description holds 4,096 characters; the live digest used to cut at
4,000 from the END, and the pool / pair lines were last, so they were the first
thing lost and a transition is only announced once (it is never re-announced).
pack_sections() splits instead of cutting: a section that does not fit carries
on in the next message (titled "(cont.)"). Only past max_messages does anything
go unsent, and then the last message says how many lines were left out.
Dependency-free and pure, so it can be tested without Discord.
"""
from __future__ import annotations

LIMIT = 4000
MAX_MESSAGES = 3


def _block(title: str, lines: list) -> str:
    return f"**{title}**\n" + "\n".join(lines)


def _size(parts: list) -> int:
    return len("\n\n".join(_block(t, ls) for t, ls in parts))


def pack_sections(sections, limit: int = LIMIT, max_messages: int = MAX_MESSAGES):
    """sections: [(title, [line, ...]), ...] in priority order.
    Returns (descriptions, left_out): one description string per message, and
    the number of lines that did not fit (already announced as '+N more' in the
    last description)."""
    messages: list = [[]]            # each message: [[head, [lines]], ...]
    left_out = 0
    full = False                     # past max_messages: only count

    def fit_alone(head, ln):
        if _size([[head, [ln]]]) <= limit:
            return ln
        room = max(limit - len(head) - 6, 20)
        return ln[:room] + "…"

    for title, lines in sections:
        started = False              # has any line of this section been placed
        open_block = None            # the block of this section in messages[-1]
        for ln in lines:
            if full:
                left_out += 1
                continue
            if open_block is not None:
                trial = messages[-1][:-1] + [[open_block[0], open_block[1] + [ln]]]
                if _size(trial) <= limit:
                    messages[-1] = trial
                    open_block = messages[-1][-1]
                    continue
            else:
                head = f"{title} (cont.)" if started else title
                trial = messages[-1] + [[head, [ln]]]
                if _size(trial) <= limit:
                    messages[-1] = trial
                    open_block = messages[-1][-1]
                    started = True
                    continue
            # does not fit in the open message: start the next one
            if len(messages) >= max_messages:
                full = True
                left_out += 1
                continue
            if not messages[-1]:     # message is empty and the line alone is too long
                head = f"{title} (cont.)" if started else title
                messages[-1] = [[head, [fit_alone(head, ln)]]]
            else:
                messages.append([])
                head = f"{title} (cont.)" if started else title
                messages[-1] = [[head, [fit_alone(head, ln)]]]
            open_block = messages[-1][-1]
            started = True
    if left_out:
        note = f"+{left_out} more line{'s' if left_out != 1 else ''} not shown"
        last = messages[-1]
        while last and _size(last + [["NOT SHOWN", [note]]]) > limit:
            last[-1][1] = last[-1][1][:-1]
            left_out += 1
            note = f"+{left_out} more line{'s' if left_out != 1 else ''} not shown"
            if not last[-1][1]:
                last.pop()
        messages[-1] = last + [["NOT SHOWN", [note]]]
    return ["\n\n".join(_block(t, ls) for t, ls in m) for m in messages if m], left_out
