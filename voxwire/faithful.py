"""The faithfulness guard for the transcript fixup (CLAUDE.md rule 4).

The fixup LLM may only fix mis-heard words. The prompt asks for that, but a prompt
is not a gate: this module checks the reply structurally and the caller keeps the
raw transcript when it fails. Pure Python, deterministic, no model.

A reply is faithful when its words align one-to-one, in order, with the input's,
and every changed word is a plausible mis-hearing of the word it replaces:

- kept         the same word (case and punctuation ignored)
- corrected    a close spelling: a throat mic drops consonants, so the heard word
               is often the real one with letters missing ("lit" → "list",
               "et" → "tests"), or differs by a letter or two ("thee" → "the")
- merged/split two heard words are one real word, or the reverse ("in to" → "into")

Nothing else. No word may be added or dropped, so "run the tests" can never become
"delete all files now" or lose a "not". When the guard is unsure it rejects, and
rejecting only costs the correction: the raw transcript is always safe to keep.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# A changed word must share at least this much of its spelling with the heard word...
MIN_SIMILARITY = 0.6
# ...or be the heard word with letters restored, keeping at least this share of them.
MIN_KEPT_LETTERS = 0.4
# A merge/split only re-spaces the same letters ("in to" → "into"), so it must be
# nearly exact; a looser bar would let a merge quietly swallow a word ("do not" → "do").
MIN_MERGE_SIMILARITY = 0.85

_TOKEN = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def close(heard: str, fixed: str) -> bool:
    """Is `fixed` a plausible mis-hearing correction of the single word `heard`?"""
    if heard == fixed:
        return True
    if SequenceMatcher(None, heard, fixed).ratio() >= MIN_SIMILARITY:
        return True
    short, long = sorted((heard, fixed), key=len)
    return (len(short) >= 2 and _is_subsequence(short, long)
            and len(short) / len(long) >= MIN_KEPT_LETTERS)


def _same_letters(heard: str, fixed: str) -> bool:
    """Do two spellings differ only slightly? Used for merges and splits."""
    return SequenceMatcher(None, heard, fixed).ratio() >= MIN_MERGE_SIMILARITY


def is_faithful(raw: str, reply: str) -> bool:
    """True if `reply` only corrects mis-heard words of `raw` (see module doc)."""
    a, b = _words(raw), _words(reply)
    if not b:
        return False
    n, m = len(a), len(b)
    # reachable[i][j]: a[:i] aligns with b[:j] using only allowed steps
    reachable = [[False] * (m + 1) for _ in range(n + 1)]
    reachable[0][0] = True
    for i in range(n + 1):
        for j in range(m + 1):
            if not reachable[i][j]:
                continue
            if i < n and j < m and close(a[i], b[j]):
                reachable[i + 1][j + 1] = True                      # kept / corrected
            if i + 1 < n and j < m and _same_letters(a[i] + a[i + 1], b[j]):
                reachable[i + 2][j + 1] = True                      # two heard → one
            if i < n and j + 1 < m and _same_letters(a[i], b[j] + b[j + 1]):
                reachable[i + 1][j + 2] = True                      # one heard → two
    return reachable[n][m]
