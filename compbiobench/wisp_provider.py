"""Wisp Science backend for the CompBioBench runner in this directory."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import run_benchmark as rb

_HERE = Path(__file__).resolve().parent
_WRAPPER = _HERE / "wisp-run.sh"


def wisp_bin() -> str:
    return os.environ.get("WISP_BIN", "").strip() or "wisp-science"


def check_wisp_installed() -> tuple[bool, str, str]:
    path = wisp_bin()
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return True, f"wisp-science at {path}", "wisp"
    found = shutil.which(path) or shutil.which("wisp-science")
    if found:
        return True, f"wisp-science at {found}", "wisp"
    return (
        False,
        "wisp-science not found. Set WISP_BIN to target/release/wisp-science.",
        "",
    )


class WispProvider(rb.LLMProvider):
    """Drive headless `wisp-science run --output jsonl` in the question workspace."""

    name = "wisp"
    default_model = os.environ.get("WISP_MODEL", "wisp")
    conda_env = "compbio-benchmark"
    install_url = "https://github.com/xuzhougeng/wisp-science"
    model_pricing: dict[str, object] = {}

    def get_model_pricing(self, model: str) -> tuple[float, float]:
        return (0.0, 0.0)

    def build_test_command(self, model: str) -> list[str]:
        return [str(_WRAPPER), model, "Reply with only the word OK"]

    def build_command(
        self,
        model: str,
        prompt: str,
        data_dir: str | None = None,
        permission_mode: str = "skip",
        workspace_dir: str = None,
        answer_output_path: str | None = None,
    ) -> list[str]:
        if not _WRAPPER.is_file():
            raise FileNotFoundError(f"missing wrapper: {_WRAPPER}")
        return [str(_WRAPPER), model, prompt]

    def parse_output(self, stdout: str, model: str) -> tuple[str, dict]:
        usage = rb.init_usage_dict(model)
        trace_parts: list[str] = []
        text_buf: list[str] = []
        think_buf: list[str] = []

        def flush_text() -> None:
            msg = rb.format_assistant_message("Wisp", "".join(text_buf))
            if msg:
                trace_parts.append(msg)
            text_buf.clear()

        def flush_think() -> None:
            msg = rb.format_assistant_message("Wisp", "".join(think_buf), thinking=True)
            if msg:
                trace_parts.append(msg)
            think_buf.clear()

        for event, line in rb.parse_jsonl_events(stdout):
            kind = event.get("type", "")
            if kind == "raw":
                flush_text()
                flush_think()
                if event.get("content", "").strip():
                    trace_parts.append(event["content"])
            elif kind == "text":
                flush_think()
                text_buf.append(event.get("delta") or "")
            elif kind == "reasoning":
                flush_text()
                think_buf.append(event.get("delta") or "")
            elif kind == "tool_call":
                flush_text()
                flush_think()
                name = event.get("name") or "unknown"
                args = event.get("arguments") or {}
                if name in ("bash", "shell") or "command" in args:
                    cmd = args.get("command") or event.get("preview") or ""
                    trace_parts.append(rb.format_bash_tool(str(cmd)))
                elif name in ("read_file", "Read", "write_file", "Write", "edit", "Edit"):
                    path = args.get("path") or args.get("file_path") or event.get("preview") or ""
                    mapped = {"read_file": "Read", "write_file": "Write", "edit": "Edit"}.get(
                        name, name
                    )
                    trace_parts.append(rb.format_tool_call(mapped, str(path)))
                else:
                    preview = event.get("preview") or ""
                    trace_parts.append(rb.format_tool_call(name, str(preview)))
            elif kind == "tool_result":
                result = rb.format_tool_result(event.get("content") or "")
                if result:
                    trace_parts.append(result)
            elif kind == "usage":
                usage["input_tokens"] += int(event.get("input_tokens") or 0)
                usage["output_tokens"] += int(event.get("output_tokens") or 0)
                usage["cached_tokens"] = int(event.get("cached_tokens") or 0)
            elif kind == "error":
                flush_text()
                flush_think()
                trace_parts.append(f"**Error:** {event.get('message') or line}")
            elif kind == "done":
                flush_text()
                flush_think()

        flush_text()
        flush_think()
        usage["estimated_cost_usd"] = self.calculate_cost(
            usage["input_tokens"], usage["output_tokens"], model
        )
        usage["cost_usd"] = usage["estimated_cost_usd"]
        return rb.build_trace_text(trace_parts, stdout), usage

    def extract_answer(
        self, stdout: str, model: str, answer_output_path: str | None = None
    ) -> str:
        chunks: list[str] = []
        parsed = False
        for event, _line in rb.parse_jsonl_events(stdout):
            if event.get("type") == "text":
                parsed = True
                chunks.append(event.get("delta") or "")
            elif event.get("type") == "error":
                parsed = True
                return rb.parse_answer(f"ERROR: {event.get('message') or 'wisp error'}")
        if parsed:
            return rb.parse_answer(rb.extract_last_non_empty_line("".join(chunks)))
        return rb.parse_answer(rb.extract_last_non_empty_line(stdout))
