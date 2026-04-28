#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 5 ]]; then
  cat <<'EOF'
Usage:
  ./scripts/join_meeting.sh "<meeting-url>" "<candidate-name>" [display-name] [bot-endpoint] [sink-endpoint]

Defaults:
  display-name: Alfred
  bot-endpoint: https://teamsbot.qmachina.com
  sink-endpoint: https://agent.qmachina.com
EOF
  exit 1
fi

MEETING_URL="$1"
CANDIDATE_NAME="$2"
DISPLAY_NAME="${3:-Alfred}"
BOT_ENDPOINT="${4:-https://teamsbot.qmachina.com}"
SINK_ENDPOINT="${5:-https://agent.qmachina.com}"
EXTRA_ARGS=()

if [[ "${JOIN_DRY_RUN:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--dry-run)
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}/python"

exec uv run python ../scripts/auto_join.py \
  --meeting-url "${MEETING_URL}" \
  --candidate-name "${CANDIDATE_NAME}" \
  --display-name "${DISPLAY_NAME}" \
  --bot-endpoint "${BOT_ENDPOINT}" \
  --sink-endpoint "${SINK_ENDPOINT}" \
  --join-mode invite_and_graph_join \
  "${EXTRA_ARGS[@]}"
