#!/usr/bin/env bash
# Voxwire leak check — fails (with non-empty output) if the working tree
# contains secret-shaped strings or any maintainer-listed private term.
#
# Why this exists: this repo is public-bound, so it must never carry employer /
# project names, internal host names, private IP ranges, or secrets. The generic,
# safe-to-name patterns are baked in below. The SPECIFIC private terms (names,
# hostnames) live only in scripts/private-denylist.txt, which is git-ignored and
# never committed — so the checker itself does not leak the very terms it guards.
#
# Usage:  ./scripts/leakcheck.sh             working tree (every commit; CI)
#         ./scripts/leakcheck.sh --history   every commit reachable from any ref,
#                                            plus commit messages. The required
#                                            gate before any public visibility
#                                            flip: a term removed from the tree
#                                            still ships in history.
# Run from anywhere; require clean output. Exit 0 clean, 1 hits, 2 error.
set -uo pipefail

case "${1:-}" in
  "") mode=tree ;;
  --history) mode=history ;;
  *) echo "usage: $0 [--history]" >&2; exit 2 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root" || exit 2

# Secret-shaped patterns that are safe to name in a public file.
# Private/internal IPv4 addresses as full dotted quads: RFC 1918 (10/8, 172.16/12,
# 192.168/16) and carrier-grade NAT 100.64/10 (tailnet addresses). The guard on
# each side stops a match inside a longer number or a version string such as
# 1.10.0.3. Plain ERE, so BSD grep (macOS) and GNU grep (CI) agree.
octet='[0-9]{1,3}'
private_ipv4="(^|[^0-9.])(10\.$octet|172\.(1[6-9]|2[0-9]|3[01])|192\.168|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7]))\.$octet\.$octet([^0-9]|$)"
patterns="$private_ipv4|sk-[a-zA-Z0-9]{16}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"

# Maintainer-local private terms (git-ignored; one grep -E fragment per line).
denylist="$root/scripts/private-denylist.txt"
if [ -f "$denylist" ]; then
  [ -r "$denylist" ] || { echo "✗ leak check ERROR — cannot read $denylist" >&2; exit 2; }
  extra="$(grep -vE '^[[:space:]]*(#|$)' "$denylist" | paste -sd '|' -)"
  [ -n "$extra" ] && patterns="$patterns|$extra"
fi

# grep exits 0 = matches, 1 = no matches, >1 = error (e.g. a malformed denylist
# regex or an unreadable file). An error must FAIL CLOSED: an empty result from a
# grep that never ran is not evidence the tree is clean.
errf="$(mktemp)" || exit 2
trap 'rm -f "$errf"' EXIT

if [ "$mode" = tree ]; then
  # Never scan these paths, nor the checker's own tooling files (which necessarily
  # mention the patterns/terms and would self-flag). Exclusions are by PATH only
  # (grep --exclude-dir / --exclude, then an exact match on the file-name field
  # below) — never by line content, so a real hit on a line that merely mentions
  # e.g. "recordings/" is still reported.
  raw="$(grep -rInE --exclude-dir=.venv --exclude-dir=.git --exclude-dir=.pytest_cache \
          --exclude-dir=__pycache__ --exclude-dir=node_modules --exclude-dir=recordings \
          --exclude-dir=dist --exclude-dir=build --exclude='*.wav' \
          -e "$patterns" . 2>"$errf")"
  rc=$?
else
  # Every blob of every commit on any ref (tooling files excluded by pathspec),
  # then every commit message. Hits read "<sha>:<path>:<line>:…" and
  # "<sha>:message:<subject>". git grep/log exit 128 on error (bad regex).
  git rev-parse --git-dir >/dev/null 2>&1 \
    || { echo "✗ leak check ERROR — --history needs a git checkout" >&2; exit 2; }
  revs="$(git rev-list --all)" || exit 2
  raw=""; rc=0
  if [ -n "$revs" ]; then
    # shellcheck disable=SC2086  # word-split on purpose: one argument per commit
    raw="$(git grep -I -n -E -e "$patterns" $revs -- . \
            ':(exclude)scripts/leakcheck.sh' ':(exclude)scripts/private-denylist.txt' 2>"$errf")"
    rc=$?
    msgs="$(git log --all -E --grep="$patterns" --format='%H:message:%s' 2>>"$errf")" || rc=2
    [ -n "$msgs" ] && raw="$raw"$'\n'"$msgs"
  fi
fi
if [ "$rc" -gt 1 ]; then
  cat "$errf" >&2
  echo "✗ leak check ERROR — grep failed (exit $rc); NOT verified clean." >&2
  echo "  Check scripts/private-denylist.txt for a malformed regex." >&2
  exit 2
fi

# Drop hits in the checker's own files, matching the path field exactly.
hits="$(printf '%s\n' "$raw" | awk '
  NF == 0 { next }
  { path = $0; sub(/:[0-9]+:.*/, "", path) }
  path != "./scripts/leakcheck.sh" && path != "./scripts/private-denylist.txt"')"
if [ -n "$hits" ]; then
  echo "$hits"
  echo "" >&2
  echo "✗ leak check FAILED — the content above must not be committed to a public repo." >&2
  exit 1
fi
echo "✓ leak check clean"
