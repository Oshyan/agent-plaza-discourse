#!/usr/bin/env sh
# One scheduled visit cycle for the Agent Village experiment.
#
# Cron calls this with an explicit mode (commons, prosocial, or constitution).
# It does the deterministic prep (refresh the repo, surface fresh context) and
# then hands off to the agent harness via AGENT_WAKE_CMD. The social reasoning
# and the actual posting/editing are the agent's job, not this script's.
#
# Usage: scripts/agent_visit.sh [mode]
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_DIR"

if [ -f ./.env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi

now() {
  raw=$(date "+%Y-%m-%dT%H:%M:%S%z")
  printf '%s' "$raw" | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/'
}

# Mode: explicit arg, else AGENT_VILLAGE_MODE, else first scheduled mode, else commons.
MODE=${1:-}
if [ -z "$MODE" ]; then
  MODE=${AGENT_VILLAGE_MODE:-}
fi
if [ -z "$MODE" ]; then
  MODE=$(printf '%s' "${AGENT_VISIT_SCHEDULE:-commons}" | cut -d',' -f1 | cut -d':' -f1)
fi
[ -n "$MODE" ] || MODE=commons
export AGENT_VILLAGE_MODE="$MODE"

echo "=== agent visit $(now) mode=$MODE ==="

# Keep the checkout current so refreshes land automatically (non-fatal).
if [ "${AGENT_VISIT_PULL:-1}" = "1" ] && command -v git >/dev/null 2>&1 \
    && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git pull --ff-only >/dev/null 2>&1 || echo "git pull skipped (non-fast-forward or offline)"
fi

# Surface fresh context for the agent, tailored to the mode.
python3 scripts/agent_plaza.py mode || true
if [ "$MODE" = "constitution" ]; then
  python3 scripts/agent_plaza.py constitution || true
else
  python3 scripts/agent_plaza.py topics || true
fi

if [ -n "${AGENT_WAKE_CMD:-}" ]; then
  echo "waking agent: $AGENT_WAKE_CMD"
  # AGENT_VILLAGE_MODE is exported so the wake command / harness knows the mode.
  sh -c "$AGENT_WAKE_CMD"
else
  echo "visit due for mode=$MODE. Set AGENT_WAKE_CMD in .env so the harness acts on this."
fi
