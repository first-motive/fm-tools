#!/usr/bin/env bash
#
# install.sh — front door for installing the fm CLI on a developer's machine.
#
# fm-tools plays two roles in the First Motive stack. As a library it ships the
# ROS-free TUI wheel any repo imports. As a tool-installer it puts the wheel's
# console entry points (fm, fm-pick) on PATH, so the cross-repo `fm` dispatcher
# is available everywhere a developer works — the counterpart to fm-ai's
# installer, which links the agent-facing skills into ~/.claude.
#
# Tool-installer role (fm-bootstrap contract): install-only, run from a local
# clone, never curl-piped, no run.sh. This front door is a thin dispatcher — it
# sources lib.sh for the brand banner and logging, then delegates the real work
# to scripts/install.sh (install) or to uv tool directly (uninstall, status).
#
# The body is wrapped in main() and called on the last line, so a truncated read
# leaves an incomplete function that never runs.

set -euo pipefail

# Resolve REPO as this script's own directory, following symlinks, so the front
# door works regardless of where the repo is cloned.
fm_repo_dir() {
  local source="${BASH_SOURCE[0]:-}" dir
  [ -n "$source" ] || { pwd; return; }
  while [ -L "$source" ]; do
    dir="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    case "$source" in /*) ;; *) source="$dir/$source" ;; esac
  done
  cd -P "$(dirname "$source")" && pwd
}

REPO="$(fm_repo_dir)"

# From a clone lib.sh sits next to this script. fm-tools is never curl-piped, so a
# direct source is enough — no fetch-and-eval fallback.
fm_load_lib() {
  local lib="$REPO/lib.sh"
  [ -f "$lib" ] || { echo "lib.sh not found at $lib" >&2; exit 1; }
  # shellcheck source=lib.sh disable=SC1091
  . "$lib"
}

usage() {
  cat <<'EOF'
install.sh — install the fm CLI (fm, fm-pick) for this developer

Usage: ./install.sh [install|uninstall|status] [--dry-run]

  install      install the fm CLI onto PATH via uv tool (default)
  uninstall    remove a previous fm CLI install
  status       report whether fm is installed and on PATH
  --dry-run    with install: print the resolved spec, install nothing

Env: FM_TOOLS_REF (git tag), FM_TOOLS_REPO  — see scripts/install.sh
EOF
}

do_install() {
  local dry="$1"
  # Delegate to the modular verb, which owns the uv tool install step.
  if [ "$dry" = "1" ]; then
    "$REPO/scripts/install.sh" --dry-run
  else
    "$REPO/scripts/install.sh"
  fi
}

do_uninstall() {
  fm_require_cmd uv
  fm_log "Removing the fm CLI"
  # `uv tool uninstall` is a no-op-with-warning if fm-tools was never installed,
  # so a stray uninstall is safe.
  uv tool uninstall fm-tools
  fm_ok "fm CLI removed."
}

do_status() {
  if fm_has_cmd fm; then
    fm_ok "fm is installed: $(command -v fm)"
  else
    fm_warn "fm is not on PATH."
    fm_warn "Run ./install.sh to install it (then 'uv tool update-shell' if needed)."
  fi
}

main() {
  fm_load_lib
  fm_banner

  local cmd="install" dry=0 arg
  for arg in "$@"; do
    case "$arg" in
      install|uninstall|status) cmd="$arg" ;;
      --dry-run) dry=1 ;;
      -h|--help) usage; return 0 ;;
      *) usage; return 1 ;;
    esac
  done

  case "$cmd" in
    install)   do_install "$dry" ;;
    uninstall) do_uninstall ;;
    status)    do_status ;;
  esac
}

main "$@"
