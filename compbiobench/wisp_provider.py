"""Wisp Science backend for the CompBioBench runner in this directory."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import run_benchmark as rb

_HERE = Path(__file__).resolve().parent
_WRAPPER = _HERE / "wisp-run.sh"


def _content_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item) for item in value)))
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value)


def wisp_answer_text(stdout: str) -> str:
    """Final answer from `wisp.agent-event.v1` JSONL.

    Headless Wisp finishes through `attempt_completion`; streamed `text`
    deltas are often absent. The last stdout line is `type=done`, not the
    answer.
    """
    answer = ""
    text_chunks: list[str] = []
    last_error = ""
    for event, line in rb.parse_jsonl_events(stdout):
        kind = event.get("type", "")
        if kind == "error":
            last_error = str(event.get("message") or line)
        elif kind == "text":
            text_chunks.append(event.get("delta") or "")
        elif kind == "tool_call" and event.get("name") == "attempt_completion":
            args = event.get("arguments") or {}
            if isinstance(args, dict):
                text = str(args.get("result") or "").strip()
                if text:
                    answer = text
        elif kind == "tool_result" and event.get("name") == "attempt_completion":
            if event.get("ok", True):
                text = (event.get("content") or "").strip()
                if text:
                    answer = text
        elif kind == "message":
            role = (event.get("role") or "").lower()
            text = _content_text(event.get("content")).strip()
            if not text:
                continue
            if role == "assistant" or (
                role == "tool" and event.get("tool_name") == "attempt_completion"
            ):
                answer = text
    if answer:
        return answer
    streamed = rb.extract_last_non_empty_line("".join(text_chunks))
    if streamed:
        return streamed
    if last_error:
        return f"ERROR: {last_error}"
    return ""


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
                if name == "attempt_completion":
                    continue
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
                if event.get("name") == "attempt_completion":
                    msg = rb.format_assistant_message("Wisp", event.get("content") or "")
                    if msg:
                        trace_parts.append(msg)
                    continue
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
        text = wisp_answer_text(stdout)
        if text.startswith("ERROR:"):
            return rb.parse_answer(text)
        if text:
            return rb.parse_answer(rb.extract_last_non_empty_line(text))
        return rb.parse_answer("ERROR: empty answer")


if __name__ == "__main__":
    done = '{"ok":true,"schema":"wisp.agent-event.v1","sequence":200,"type":"done"}'
    sample = "\n".join(
        [
            '{"type":"text","delta":"working..."}',
            '{"type":"tool_call","name":"attempt_completion","arguments":{"result":"BRCA1"}}',
            '{"type":"tool_result","name":"attempt_completion","ok":true,"content":"BRCA1"}',
            '{"type":"message","role":"tool","tool_name":"attempt_completion","content":"BRCA1"}',
            done,
        ]
    )
    assert wisp_answer_text(sample) == "BRCA1", wisp_answer_text(sample)
    assert wisp_answer_text(done) == ""
    assert WispProvider().extract_answer(done, "m").startswith("ERROR")
    assert WispProvider().extract_answer(sample, "m") == "BRCA1"
    multiline = '{"type":"tool_result","name":"attempt_completion","ok":true,"content":"note\\nTP53"}'
    assert WispProvider().extract_answer(multiline, "m") == "TP53"
    parts = '{"type":"message","role":"assistant","content":[{"type":"text","text":"chr2:123"}]}'
    assert wisp_answer_text(parts) == "chr2:123"
    print("ok")
