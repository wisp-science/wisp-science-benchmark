"""NatureBench adapter for Wisp Science.

Drop into a NatureBench clone as ``agent/wisp_adapter.py`` (see
``install_adapter.py``). Reuses the official Claude task prompt so the
evaluation protocol matches the built-in CLIs.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from .adapter import REGISTRY, AgentAdapter, AgentRunContext
from .claude import ClaudeAgent

_RESUME_PROMPT = (
    "[RESUME NOTICE] You were previously interrupted on this task. "
    "The /workspace contents and any prior /evaluate submissions are preserved. "
    "Continue from where you left off. "
    "**Use the full remaining time budget**: keep iterating, profiling, and "
    "refining until `/time_remaining` is close to zero. Do **not** exit early "
    "just because you have a working baseline. Check `/time_remaining` first."
)

_CONTAINER_BIN = "/opt/wisp/wisp-science"
_CONTAINER_ROOT = "/opt/wisp/src"


class WispAdapter(AgentAdapter):
    name = "wisp"

    def system_prompt(self, ctx: AgentRunContext) -> str:
        if ctx.is_resume:
            return _RESUME_PROMPT
        return ClaudeAgent(model_name=ctx.model, mode=ctx.mode).build_system_prompt(
            {
                "eval_service_url": ctx.eval_service_url,
                "eval_token": ctx.eval_token,
                "time_limit_minutes": ctx.time_limit_minutes,
            }
        )

    def build_command(self, ctx: AgentRunContext) -> List[str]:
        # Prompt is argv "$1" so solve.py quoting is preserved (same pattern as Lumen).
        script = (
            "set -euo pipefail\n"
            "mkdir -p /workspace\n"
            f'test -x "{_CONTAINER_BIN}" || {{ echo "wisp-science missing at {_CONTAINER_BIN}" >&2; exit 127; }}\n'
            f'export WISP_BIN="{_CONTAINER_BIN}"\n'
            f'export WISP_ROOT="{_CONTAINER_ROOT}"\n'
            f'export WISP_MODEL="{ctx.model}"\n'
            f'exec "{_CONTAINER_BIN}" run --output jsonl "$1" '
            "> /workspace/wisp.jsonl 2> /workspace/wisp.err\n"
        )
        return ["bash", "-lc", script, "wisp", ctx.system_prompt]

    def docker_mounts(self, ctx: AgentRunContext) -> List[str]:
        mounts: List[str] = []
        host_bin = os.environ.get("WISP_BIN", "").strip()
        host_root = os.environ.get("WISP_ROOT", "").strip()
        if host_bin:
            mounts.extend(["-v", f"{host_bin}:{_CONTAINER_BIN}:ro"])
        if host_root:
            mounts.extend(["-v", f"{host_root}:{_CONTAINER_ROOT}:ro"])
        return mounts

    def extra_env(self, ctx: AgentRunContext) -> List[str]:
        env: List[str] = [
            "-e", f"WISP_BIN={_CONTAINER_BIN}",
            "-e", f"WISP_ROOT={_CONTAINER_ROOT}",
            "-e", f"WISP_MODEL={ctx.model}",
        ]
        for key in (
            "WISP_PROVIDER",
            "WISP_API_URL",
            "WISP_API_KEY",
            "WISP_VISION",
            "WISP_REASONING_EFFORT",
            "WISP_MAX_TOKENS",
            "WISP_MAX_ITER",
        ):
            val = os.environ.get(key)
            if val:
                env.extend(["-e", f"{key}={val}"])
        return env

    def transcript_path(self, task_out_dir: Path) -> Optional[Path]:
        workspace = task_out_dir / "workspace" / "wisp.jsonl"
        if workspace.is_file():
            return workspace
        host = task_out_dir / "wisp.jsonl"
        return host if host.is_file() else None


REGISTRY.register(WispAdapter())
