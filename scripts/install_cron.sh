#!/usr/bin/env sh
# Install (or remove) the cron schedule for recurring Agent Village visits.
#
# Uses plain cron (works on Linux and macOS; not launchd). The schedule comes
# from AGENT_VISIT_SCHEDULE: a comma list of modes, each run once per day by
# default. Append ":N" to run a mode N times per day, e.g. "prosocial:2".
# Default: commons,prosocial,constitution (each once daily), staggered across
# the day so the three turns do not fire at once.
#
# Usage:
#   scripts/install_cron.sh
#   scripts/install_cron.sh --schedule "commons,prosocial:2,constitution"
#   scripts/install_cron.sh --dry-run
#   scripts/install_cron.sh --uninstall
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO_DIR"

if [ -f ./.env ]; then
  # shellcheck disable=SC1091
  . ./.env
fi

MARKER="# agent-village-commons visit"
SCHEDULE=${AGENT_VISIT_SCHEDULE:-commons,prosocial,constitution}
ACTION="install"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --schedule) SCHEDULE=$2; shift 2 ;;
    --schedule=*) SCHEDULE=${1#*=}; shift ;;
    --dry-run) ACTION="dry-run"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Build the cron lines from the schedule.
build_lines() {
  OLD_IFS=$IFS
  IFS=','
  # shellcheck disable=SC2086
  set -- $SCHEDULE
  IFS=$OLD_IFS
  n=$#
  [ "$n" -ge 1 ] || return 0
  base_step=$(( 24 / n )); [ "$base_step" -ge 1 ] || base_step=1
  i=0
  for entry in "$@"; do
    mode=${entry%%:*}
    times=${entry#*:}
    [ "$times" = "$entry" ] && times=1
    case "$times" in (*[!0-9]*|'') times=1 ;; esac
    [ "$times" -ge 1 ] || times=1
    minute=$(( (i * 15) % 60 ))
    base_hour=$(( i * base_step ))
    step_hour=$(( 24 / times )); [ "$step_hour" -ge 1 ] || step_hour=1
    j=0
    while [ "$j" -lt "$times" ]; do
      hour=$(( (base_hour + j * step_hour) % 24 ))
      echo "$minute $hour * * * cd $REPO_DIR && /bin/sh scripts/agent_visit.sh $mode >> $REPO_DIR/visits.log 2>&1 $MARKER"
      j=$(( j + 1 ))
    done
    i=$(( i + 1 ))
  done
}

LINES=$(build_lines)

if [ "$ACTION" = "dry-run" ]; then
  printf '%s\n' "$LINES"
  exit 0
fi

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab not found. Add these lines to your scheduler manually:" >&2
  printf '%s\n' "$LINES" >&2
  exit 1
fi

EXISTING=$(crontab -l 2>/dev/null | grep -vF "$MARKER" || true)

if [ "$ACTION" = "uninstall" ]; then
  printf '%s\n' "$EXISTING" | sed '/^$/d' | crontab -
  echo "Removed Agent Village cron schedule."
  exit 0
fi

{ printf '%s\n' "$EXISTING" | sed '/^$/d'; printf '%s\n' "$LINES"; } | crontab -
echo "Installed Agent Village cron schedule ($SCHEDULE):"
printf '%s\n' "$LINES" | sed 's/^/  /'
echo
echo "Visits log: $REPO_DIR/visits.log"
if [ -z "${AGENT_WAKE_CMD:-}" ]; then
  echo "Note: AGENT_WAKE_CMD is empty, so visits will only log that they are due."
  echo "Set AGENT_WAKE_CMD in .env to the command that wakes this agent for a visit."
fi
