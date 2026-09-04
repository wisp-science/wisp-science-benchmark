#!/usr/bin/env bash
# CompBioBench wrapper: wisp-run.sh <model> <prompt>
# WISP_MODEL is set from the runner's -m so one command line selects the model.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: wisp-run.sh <model> <prompt>" >&2
  exit 2
fi

model="$1"
prompt="$2"

if [[ -z "${WISP_BIN:-}" ]]; then
  echo "WISP_BIN is not set" >&2
  exit 2
fi
if [[ ! -x "$WISP_BIN" ]]; then
  echo "WISP_BIN is not executable: $WISP_BIN" >&2
  exit 2
fi

export WISP_MODEL="$model"
exec "$WISP_BIN" run --output jsonl "$prompt"
