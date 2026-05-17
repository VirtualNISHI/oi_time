#!/usr/bin/env bash
# Restore / save the .last_posted.json state file on a dedicated orphan-style branch.
# Pattern borrowed from polymarket-smart-money.
#
#   ./scripts/_ci_state.sh restore   # pulls bot-state:last_posted.json -> $STATE_FILE
#   ./scripts/_ci_state.sh save      # force-pushes $STATE_FILE -> bot-state branch
#
# Why a separate branch:
#   Keeps main's history clean — no "chore: update state" commit noise every 6h.
#
# Required env (provided automatically by GitHub Actions):
#   GITHUB_REPOSITORY   owner/repo
#   GH_TOKEN            a token with `contents: write` (use ${{ secrets.GITHUB_TOKEN }})
#
# Optional env:
#   STATE_BRANCH        default: bot-state
#   STATE_FILE          default: .last_posted.json
#   GITHUB_WORKSPACE    default: $PWD (set by Actions)

set -euo pipefail

ACTION="${1:-}"
STATE_BRANCH="${STATE_BRANCH:-bot-state}"
STATE_FILE="${STATE_FILE:-.last_posted.json}"
WORKSPACE="${GITHUB_WORKSPACE:-$PWD}"
STATE_BASENAME="$(basename "$STATE_FILE")"

case "$ACTION" in
  restore)
    mkdir -p "$(dirname "$WORKSPACE/$STATE_FILE")"
    if git ls-remote --exit-code origin "$STATE_BRANCH" >/dev/null 2>&1; then
      git fetch --depth=1 origin "$STATE_BRANCH"
      if git show "FETCH_HEAD:$STATE_BASENAME" > "$WORKSPACE/$STATE_FILE" 2>/dev/null; then
        echo "restored $STATE_FILE from origin/$STATE_BRANCH ($(stat -c%s "$WORKSPACE/$STATE_FILE") bytes)"
      else
        rm -f "$WORKSPACE/$STATE_FILE"
        echo "branch $STATE_BRANCH exists but has no $STATE_BASENAME; starting fresh"
      fi
    else
      echo "$STATE_BRANCH does not exist yet; starting fresh"
    fi
    ;;

  save)
    if [[ ! -f "$WORKSPACE/$STATE_FILE" ]]; then
      echo "no state at $STATE_FILE; nothing to save"
      exit 0
    fi
    : "${GITHUB_REPOSITORY:?required}"
    : "${GH_TOKEN:?required}"

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    pushd "$tmp" >/dev/null

    git init -q -b "$STATE_BRANCH"
    git config user.email "oi-time-bot@users.noreply.github.com"
    git config user.name  "oi-time-bot"

    cp "$WORKSPACE/$STATE_FILE" "$STATE_BASENAME"
    git add "$STATE_BASENAME"
    git -c commit.gpgsign=false commit -q -m "state @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"

    git remote add origin "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
    git push --force origin "$STATE_BRANCH"

    popd >/dev/null
    echo "pushed $STATE_FILE -> origin/$STATE_BRANCH"
    ;;

  *)
    echo "usage: $0 restore|save" >&2
    exit 2
    ;;
esac
