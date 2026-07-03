#!/usr/bin/env bash
#
# install.sh (verb) — put the fm CLI on the developer's PATH.
#
# fm-tools is a tool-installer in the fm-bootstrap sense: install-only, run from a
# local clone, never curl-piped. This verb installs the wheel's console entry
# points (fm, fm-pick) as an isolated uv tool, so `fm` resolves on PATH for every
# developer the way skills resolve in ~/.claude after fm-ai's installer.
#
# It is standalone — `./scripts/install.sh` works without the install.sh front
# door — so it sources lib.sh itself and carries its own strict mode and --help.
#
# The install source is a pinned git tag, not the working tree: a developer gets
# a reproducible release, and upgrades are explicit (bump the tag, re-run, or
# `uv tool upgrade fm-tools`). Override the tag with FM_TOOLS_REF; the default is
# derived from pyproject.toml's version so it tracks the wheel.

set -euo pipefail

fm_script_dir() {
  local source="${BASH_SOURCE[0]:-}" dir
  [ -n "$source" ] || { pwd; return; }
  while [ -L "$source" ]; do
    dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    case "$source" in /*) ;; *) source="$dir/$source" ;; esac
  done
  cd -P "$(dirname "$source")" && pwd
}

# A verb lives in scripts/, so lib.sh is one directory up.
fm_load_lib() {
  local lib
  lib="$(fm_script_dir)/../lib.sh"
  # shellcheck source=../lib.sh disable=SC1091
  . "$lib"
}

FM_TOOLS_REPO="${FM_TOOLS_REPO:-first-motive/fm-tools}"

usage() {
  cat <<'EOF'
install.sh — install the fm CLI (fm, fm-pick) onto PATH via uv tool

Usage: ./scripts/install.sh [--dry-run] [-h]

  --dry-run    print the resolved install spec and exit; install nothing
  -h, --help   show this help

Env:
  FM_TOOLS_REF   git tag to install (default: v<version> from pyproject.toml)
  FM_TOOLS_REPO  owner/repo to install from (default: first-motive/fm-tools)
EOF
}

# Read the wheel version from pyproject.toml so the default install tag tracks the
# release without a second place to bump. Matches the first `version = "X.Y.Z"`.
fm_pyproject_version() {
  local pyproject
  pyproject="$(fm_script_dir)/../pyproject.toml"
  [ -f "$pyproject" ] || { fm_err "pyproject.toml not found at repo root: $pyproject"; return 1; }
  sed -n 's/^version = "\([^"]*\)".*/\1/p' "$pyproject" | head -n1
}

# Resolve the install spec (repo + ref) with no side effects, so --dry-run and
# the test suite can inspect exactly what would be installed without touching uv.
fm_resolve_spec() {
  local ref
  ref="${FM_TOOLS_REF:-v$(fm_pyproject_version)}"
  [ -n "$ref" ] && [ "$ref" != "v" ] || { fm_err "could not resolve install tag; set FM_TOOLS_REF"; return 1; }
  printf 'fm-tools @ git+https://github.com/%s@%s\n' "$FM_TOOLS_REPO" "$ref"
}

do_install() {
  local dry="$1" spec
  spec="$(fm_resolve_spec)" || return 1

  if [ "$dry" = "1" ]; then
    fm_log "$spec"
    return 0
  fi

  fm_require_cmd uv
  fm_log "Installing fm CLI: $spec"

  # --force so a re-run upgrades in place instead of erroring on an existing tool.
  uv tool install --force "$spec"

  # uv installs into its own tool bin; that dir is on PATH only after the user has
  # run `uv tool update-shell` once. Point them at it rather than editing a
  # profile ourselves — uv owns its bin, so uv owns the PATH wiring.
  if fm_has_cmd fm; then
    fm_ok "fm CLI installed; \`fm list\` is ready."
  else
    fm_warn "fm installed, but not yet on PATH. Run: uv tool update-shell"
    fm_warn "then restart your shell."
  fi
}

main() {
  local dry=0
  case "${1:-}" in
    -h|--help) usage; return 0 ;;
    --dry-run) dry=1 ;;
    "") ;;
    *) usage; return 1 ;;
  esac

  fm_load_lib
  do_install "$dry"
}

main "$@"
