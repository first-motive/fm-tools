#!/usr/bin/env bash
# Refuse a source change that would ship under a version already released.
#
# `install.sh` resolves its install tag from the version in pyproject.toml, and
# `uv tool install --force` reuses its cached wheel when the version has not
# moved. So a merge that edits src/ without bumping the version is invisible
# twice over: the installer fetches the old tag, and uv serves the old wheel.
# Nothing fails, and the change reaches nobody.
#
# That has now happened twice. The kernel merge left pyproject at 0.4.1 and went
# unnoticed until somebody wondered why a new verb was missing; #27 added a
# field to the `fm commands` payload and left 0.6.0 in place, which would have
# left the hook that depends on it inert on every machine. Both were caught by a
# person looking; this is the same catch, made by machinery.
#
# The comparison is against the newest released tag rather than the pull
# request's base, so the rule holds the same way on a branch, on main, and on a
# re-run months later: if src/ has moved since the last release, the version
# must have moved too.
#
# Usage: check-version-bump.sh [repo-dir]
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

fail() { printf 'error: %s\n' "$1" >&2; exit 1; }

[ -f pyproject.toml ] || fail "no pyproject.toml in $ROOT"

VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)"
[ -n "$VERSION" ] || fail "pyproject.toml has no version line"

# Newest release by version order, not by tag date: a patch cut after a minor
# would otherwise look like the latest.
TAG="$(git tag --list 'v*' --sort=-v:refname | head -1)"
if [ -z "$TAG" ]; then
  echo "ok: nothing released yet (no v* tag) — version $VERSION is free to be anything"
  exit 0
fi

# Only the shipped package counts. Tests, workflows, and docs change constantly
# and ship nothing; holding them to a version bump would train everyone to bump
# meaninglessly, which is how a guard stops meaning anything.
if git diff --quiet "$TAG" HEAD -- src/ 2>/dev/null; then
  echo "ok: src/ is unchanged since $TAG"
  exit 0
fi

RELEASED="${TAG#v}"
if [ "$VERSION" = "$RELEASED" ]; then
  cat >&2 <<EOF
error: src/ has changed since $TAG, but pyproject.toml still declares $VERSION.

  install.sh resolves its install tag from this version, so it would fetch
  $TAG — the release without these changes. uv would then serve its cached
  wheel for the same version. The change would reach nobody, and nothing
  would fail.

  Bump the version in pyproject.toml, then cut the tag after merge.

  Changed under src/:
$(git diff --name-only "$TAG" HEAD -- src/ | sed 's/^/    /')
EOF
  exit 1
fi

# A version that sorts below the newest release is not a bump — it is a rollback
# nobody meant, and it resolves to a tag that may not exist.
NEWEST="$(printf '%s\n%s\n' "$RELEASED" "$VERSION" | sort -V | tail -1)"
if [ "$NEWEST" != "$VERSION" ]; then
  fail "pyproject.toml declares $VERSION, which is older than the released $TAG"
fi

echo "ok: src/ changed since $TAG and the version moved $RELEASED -> $VERSION"
