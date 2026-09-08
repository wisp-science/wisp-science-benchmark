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
# Headless Wisp runs `uv pip install` with captured output and no timeout.
# A blocked pypi.org hangs the question with zero logs; cap HTTP waits.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-30}"
if [[ -n "${COMPBIO_CACHE_DIR:-}" ]]; then
  export HF_HOME="${HF_HOME:-$COMPBIO_CACHE_DIR/models/huggingface}"
  export HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
  export TRANSFORMERS_CACHE="${TRANSFORMERS_CACHE:-$HF_HOME/transformers}"
  export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}"
  export SINGULARITY_CACHEDIR="${SINGULARITY_CACHEDIR:-$COMPBIO_CACHE_DIR/singularity}"
  export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$COMPBIO_CACHE_DIR/singularity}"
  export SINGULARITY_PULLFOLDER="${SINGULARITY_PULLFOLDER:-$COMPBIO_CACHE_DIR/singularity}"
  export APPTAINER_PULLFOLDER="${APPTAINER_PULLFOLDER:-$COMPBIO_CACHE_DIR/singularity}"
fi
exec "$WISP_BIN" run --output jsonl "$prompt"
