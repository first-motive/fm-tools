#!/usr/bin/env bash
# cut-release.sh — cut this repo's release tag, the one verb `fm release --cut`
# delegates to.
#
#   ./scripts/dev/cut-release.sh              # print the plan, change nothing
#   ./scripts/dev/cut-release.sh --apply      # create and push the tag
#   ./scripts/dev/cut-release.sh --set 0.8.0  # prepare the bump commit for a PR
#
# fm-tools declared no release script, so `fm release --repo fm-tools --cut`
# refused and every tag here was cut by hand — in the repo that owns the gate.
# That is the second half of #23, and the reason this exists.
#
# The version is not chosen here. It is read from pyproject.toml, which is also
# where install.sh resolves its install tag from, so the tag and the version the
# installer asks for can never name different releases. Cutting a tag is
# therefore only ever the act of publishing a version main already declares.
#
# The two halves land by different routes. The bump edits a tracked file, so it
# goes through a pull request like any other change. The tag is cut afterwards
# onto the merged commit — which is why --apply tags origin/main's tip rather
# than local HEAD.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

log()  { printf '==> %s\n' "$1"; }
ok()   { printf '    ok  %s\n' "$1"; }
err()  { printf 'error: %s\n' "$1" >&2; }

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# The version pyproject declares, read from the remote's main rather than the
# working tree: a maintainer's checkout may sit on a branch, a detached tag, or
# an unmerged bump, and none of those are what an installer would resolve.
remote_version() {
  git show origin/main:pyproject.toml 2>/dev/null \
    | sed -n 's/^version = "\([^"]*\)"/\1/p' | head -1
}

# Prepare the bump commit. Deliberately does not push or tag: this edits a
# tracked file, so it belongs in a pull request, and the tag belongs on the
# commit that merges.
set_version() {  # version
  local version="$1" branch
  case "$version" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) err "version must look like 0.8.0, got '$version'"; return 1 ;;
  esac
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$branch" = "main" ]; then
    err "refusing to commit a bump on main — branch first, then open a pull request"
    return 1
  fi
  if [ -n "$(git status --porcelain)" ]; then
    err "the tree has uncommitted changes — commit or stash them first"
    return 1
  fi
  local current
  current="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)"
  if [ "$current" = "$version" ]; then
    err "pyproject.toml already declares $version"
    return 1
  fi
  # Only the [project] version line, which is the first one: a dependency pin
  # further down the file is shaped the same way and is not ours to rewrite.
  local tmp; tmp="$(mktemp)"
  awk -v v="$version" '!done && /^version = "/ { sub(/"[^"]*"/, "\"" v "\""); done=1 } { print }' \
    pyproject.toml > "$tmp"
  mv "$tmp" pyproject.toml
  git add pyproject.toml
  git commit -q -m "chore: bump version to $version"
  ok "bump committed on $branch — open a pull request, then re-run with --apply"
}

main() {
  local apply=0 set_to=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --apply) apply=1; shift ;;
      --set) set_to="${2:-}"; shift 2 ;;
      -h|--help) usage; return 0 ;;
      *) err "unknown argument '$1'"; usage >&2; return 1 ;;
    esac
  done

  if [ -n "$set_to" ]; then
    set_version "$set_to"
    return $?
  fi

  if ! git fetch -q --tags origin main 2>/dev/null; then
    err "could not fetch origin — check network and access"
    return 1
  fi

  local version tag tip tagged
  version="$(remote_version)"
  [ -n "$version" ] || { err "could not read the version from origin/main:pyproject.toml"; return 1; }
  tag="v$version"
  tip="$(git rev-parse origin/main)"

  # Already released. Re-cutting would move a tag an installer may already
  # resolve, and uv would keep serving the wheel it cached for it.
  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    tagged="$(git rev-parse "$tag^{commit}")"
    if [ "$tagged" = "$tip" ]; then
      ok "$tag is already the main tip (${tip:0:7}) — nothing to cut"
      return 0
    fi
    err "$tag already exists at ${tagged:0:7}, but main is at ${tip:0:7}"
    printf '    bump the version with --set and merge that first; a released tag is never moved\n' >&2
    return 1
  fi

  if [ "$apply" = 0 ]; then
    log "plan: tag $tag at main ${tip:0:7}"
    printf '    re-run with --apply to create and push it\n'
    return 0
  fi

  log "tagging $tag at main ${tip:0:7} ..."
  git tag -a "$tag" "$tip" -m "$tag"
  git push -q origin "$tag"
  ok "$tag pushed — ./install.sh now resolves it"
}

main "$@"
