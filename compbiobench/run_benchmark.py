#!/usr/bin/env python3
"""
Benchmark Runner Script

Vendored from https://github.com/Genentech/compbiobench-runner (MIT) with a
first-class `wisp` backend. Upstream license: LICENSE.compbiobench-runner.

Runs benchmark questions through various LLM CLIs (Claude, Codex, Gemini, Wisp).

Architecture:
    - LLMProvider: Abstract base class for LLM CLI providers
    - ClaudeProvider, CodexProvider, GeminiProvider: Concrete implementations
    - Trace formatting helpers: Shared utilities for consistent markdown output
    - Each question runs in an isolated conda environment for reproducibility

Key Features:
    - Rich trace output: All providers produce consistently formatted markdown traces
      with tool calls, results, and reasoning chains
    - Cost tracking: Automatic token counting and cost calculation per provider
    - Parallel execution: Run multiple questions concurrently with isolation
    - Resumable: Skip completed questions when resuming failed runs

Prerequisites:
    1. Conda installed (Miniconda or Anaconda)
    2. Node.js installed (required for Gemini and Codex CLIs)
    3. LLM CLIs installed and authenticated:
       - Claude: Install claude-code and run `claude login`
       - Codex: Install codex CLI and run `codex auth`
       - Gemini: Install gemini CLI and run `gemini auth`

Usage:
    python run_benchmark.py run-all [options]  # Run all LLMs and merge results
    python run_benchmark.py run [options]      # Run benchmark with a single LLM
    python run_benchmark.py merge [options]    # Merge results from multiple runs

Examples:
    # Run all LLMs
    python run_benchmark.py run-all

    # Run single LLM
    python run_benchmark.py run --llm claude

    # Keep cloned environments for debugging
    python run_benchmark.py run --llm claude --keep-envs

    # Resume a specific run
    python run_benchmark.py run --llm claude --resume claude_opus-4-6_20260329_120000

    # Prepare benchmark CSV from xlsx
    python run_benchmark.py prepare -i questions.xlsx -o benchmark.csv
"""

import argparse
import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import cast
import threading
import pandas as pd

# python run_benchmark.py registers as __main__, not run_benchmark.
# wisp_provider does `import run_benchmark`; without this alias that
# re-executes the file and deadlocks on WispProvider.
if __name__ == "__main__":
    sys.modules.setdefault("run_benchmark", sys.modules["__main__"])


# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================

# Global state for tracking running processes and cleanup
_shutdown_requested = False
_active_processes: list[subprocess.Popen] = []
_active_conda_envs: list[str] = []
_process_lock = threading.Lock()


def request_shutdown():
    """Signal that shutdown has been requested."""
    global _shutdown_requested
    _shutdown_requested = True


def is_shutdown_requested() -> bool:
    """Check if shutdown has been requested."""
    return _shutdown_requested


def register_process(proc: subprocess.Popen) -> None:
    """Register a process for cleanup on shutdown."""
    with _process_lock:
        _active_processes.append(proc)


def unregister_process(proc: subprocess.Popen) -> None:
    """Unregister a process after it completes."""
    with _process_lock:
        if proc in _active_processes:
            _active_processes.remove(proc)


def register_conda_env(env_name: str) -> None:
    """Register a conda environment for cleanup on shutdown."""
    with _process_lock:
        _active_conda_envs.append(env_name)


def unregister_conda_env(env_name: str) -> None:
    """Unregister a conda environment after cleanup."""
    with _process_lock:
        if env_name in _active_conda_envs:
            _active_conda_envs.remove(env_name)


def cleanup_all_processes() -> None:
    """Kill all active processes."""
    with _process_lock:
        procs = list(_active_processes)
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


def cleanup_all_conda_envs(logger: logging.Logger | None = None) -> None:
    """Remove all registered conda environments."""
    with _process_lock:
        envs = list(_active_conda_envs)
    for env_name in envs:
        try:
            if logger:
                logger.debug(f"Cleaning up conda env: {env_name}")
            subprocess.run(
                ["conda", "env", "remove", "-n", env_name, "-y", "-q"],
                capture_output=True,
                timeout=60
            )
        except Exception:
            pass


def signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    if is_shutdown_requested():
        # Second signal - force exit
        print("\nForce exit requested. Terminating immediately...")
        sys.exit(1)

    print("\n\nShutdown requested (Ctrl+C). Cleaning up...")
    request_shutdown()

    # Kill all running LLM processes
    cleanup_all_processes()

    # Clean up conda environments
    cleanup_all_conda_envs()

    print("Cleanup complete. Exiting.")
    sys.exit(0)


# ============================================================================
# TRACE FORMATTING HELPERS
# ============================================================================
#
# These utilities provide consistent markdown formatting across all LLM providers.
# Each provider's parse_output() method uses these to build human-readable traces.
#
# Trace format conventions:
#   - **Provider:** message         - Assistant reasoning/explanations
#   - **Provider (thinking):** msg  - Internal reasoning (e.g., Codex)
#   - **Tool: Name** `detail`       - Tool calls with parameters
#   - **Result:** ```output```      - Tool outputs in code blocks
#
# ============================================================================

# Maximum length for tool output in traces (prevents huge traces)
MAX_OUTPUT_LENGTH = 2000


def format_tool_call(tool_name: str, detail: str = "") -> str:
    """Format a tool call for the trace.

    Args:
        tool_name: Normalized tool name (Bash, Read, Write, Edit, WebSearch, etc.)
        detail: Optional detail like file path or query

    Returns:
        Formatted markdown string
    """
    if detail:
        return f"**Tool: {tool_name}** `{detail}`"
    return f"**Tool: {tool_name}**"


def format_bash_tool(command: str) -> str:
    """Format a bash command for the trace."""
    return f"**Tool: Bash**\n```bash\n{command}\n```"


def format_tool_result(output: str, truncate: bool = True) -> str:
    """Format a tool result for the trace.

    Args:
        output: The tool output
        truncate: Whether to truncate long outputs

    Returns:
        Formatted markdown string
    """
    output = output.strip()
    if not output:
        return ""
    if truncate and len(output) > MAX_OUTPUT_LENGTH:
        output = output[:MAX_OUTPUT_LENGTH] + "\n... (truncated)"
    return f"**Result:**\n```\n{output}\n```"


def format_assistant_message(provider_name: str, text: str, thinking: bool = False) -> str:
    """Format an assistant message for the trace.

    Args:
        provider_name: The LLM provider name (Claude, Codex, Gemini)
        text: The message text
        thinking: Whether this is a thinking/reasoning step

    Returns:
        Formatted markdown string
    """
    text = text.strip()
    if not text:
        return ""
    if thinking:
        return f"**{provider_name} (thinking):** {text}"
    return f"**{provider_name}:** {text}"


def parse_jsonl_events(stdout: str):
    """Parse JSONL output into events, yielding (event, line) tuples.

    Yields JSON events from newline-delimited JSON output.
    Non-JSON lines are yielded as {"type": "raw", "content": line}.
    """
    for line in stdout.strip().split('\n'):
        if not line.strip():
            continue
        try:
            yield json.loads(line), line
        except json.JSONDecodeError:
            if line.strip():
                yield {"type": "raw", "content": line}, line


def build_trace_text(trace_parts: list[str], fallback: str = "") -> str:
    """Join trace parts into final markdown text."""
    return "\n\n".join(trace_parts) if trace_parts else fallback


def init_usage_dict(model: str, **extra_fields) -> dict:
    """Initialize a standard usage dictionary.

    Args:
        model: The model name
        **extra_fields: Additional provider-specific fields

    Returns:
        Usage dictionary with standard fields
    """
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reported_cost_usd": None,
        "estimated_cost_usd": 0.0,
        "cost_usd": 0.0,
        "model": model,
        "models": [],
    }
    usage.update(extra_fields)
    return usage


# ============================================================================
# LLM PROVIDER CLASSES
# ============================================================================

class LLMProvider(ABC):
    """Abstract base class for LLM CLI providers."""

    name: str = ""
    default_model: str = ""
    conda_env: str = ""
    install_url: str = ""

    # Model pricing: provider-specific model pricing maps.
    model_pricing: dict[str, object] = {}

    @abstractmethod
    def build_command(self, model: str, prompt: str, data_dir: str | None = None, permission_mode: str = "skip",
            workspace_dir: str = None, answer_output_path: str | None = None) -> list[str]:
        """Build the CLI command to execute."""
        pass

    @abstractmethod
    def parse_output(self, stdout: str, model: str) -> tuple[str, dict]:
        """
        Parse CLI output and extract trace text and usage info.
        Returns (trace_text, usage_dict).
        usage_dict should contain: input_tokens, output_tokens, cost_usd, model
        """
        pass

    @abstractmethod
    def extract_answer(self, stdout: str, model: str, answer_output_path: str | None = None) -> str:
        """Extract final answer from provider-specific output channels."""
        pass

    @abstractmethod
    def build_test_command(self, model: str) -> list[str]:
        """Build a minimal test command to verify model availability."""
        pass

    def check_model_available(self, model: str, timeout: int = 60, model_reasoning_effort: str | None = None) -> tuple[bool, str]:
        """Test if a model is accessible by sending a minimal prompt.

        Sends a simple test prompt to verify the model is reachable and working.
        Catches API errors, auth issues, Bedrock connectivity problems, rate limits, etc.

        Args:
            model: The model name to test
            timeout: Timeout in seconds for the test
            model_reasoning_effort: Optional reasoning effort setting for supporting models

        Returns:
            (success, message) tuple
        """
        cmd = self.build_test_command(model)
        if self.name == "claude" and cmd and cmd[0] == "claude":
            assert model_reasoning_effort is not None
            cmd = cmd[:1] + ["--effort", model_reasoning_effort] + cmd[1:]
        if self.name == "codex" and len(cmd) >= 2 and cmd[0] == "codex" and cmd[1] == "exec":
            assert model_reasoning_effort is not None
            cmd = cmd[:2] + ["-c", f"model_reasoning_effort={model_reasoning_effort}"] + cmd[2:]

        # Remove CLAUDECODE env var to allow nested sessions
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            if result.returncode == 0:
                return True, f"Model {model!r} is accessible"

            # Check stderr and stdout for common error patterns
            output = (result.stderr + result.stdout).lower()
            if "not found" in output or "invalid model" in output or "does not exist" in output:
                return False, f"Model {model!r} not found or invalid"
            if "authentication" in output or "unauthorized" in output or "api key" in output:
                return False, f"Authentication error for model {model!r}"
            if "rate limit" in output or "quota" in output:
                return False, f"Rate limit or quota error for model {model!r}"
            if "timeout" in output or "timed out" in output:
                return False, f"API timeout for model {model!r}"

            # Generic failure with truncated error message
            error_msg = (result.stderr or result.stdout or "unknown error")[:200]
            return False, f"Model test failed: {error_msg}"

        except subprocess.TimeoutExpired:
            return False, f"Model {model!r} test timed out after {timeout}s (API may be unreachable)"
        except FileNotFoundError:
            return False, f"CLI {self.name!r} not found"
        except Exception as e:
            return False, f"Error testing model {model!r}: {e}"

    def get_model_pricing(self, model: str) -> tuple[float, float]:
        """Get pricing per 1M tokens (input, output) for a model."""
        if model in self.model_pricing:
            return self.model_pricing[model]
        raise ValueError(
            f"No pricing configured for model {model!r} in provider {self.name!r}. "
            "Add it to model_pricing before running benchmarks."
        )

    def calculate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """Calculate cost in USD given token counts and model."""
        input_price, output_price = self.get_model_pricing(model)
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return round(cost, 6)


class ClaudeProvider(LLMProvider):
    """Claude Code CLI provider.

    Model information from: https://platform.claude.com/docs/en/about-claude/models/overview
    Pricing from: https://platform.claude.com/docs/en/about-claude/pricing
    Last updated: 2026-02
    """

    name = "claude"
    default_model = "claude-sonnet-4-5"
    conda_env = "claude-cli"
    install_url = "https://docs.anthropic.com/en/docs/claude-code"

    # Model pricing in USD per 1M tokens.
    # Keep only actively used models for now.
    model_pricing = {
        "claude-opus-4-5": {
            "input_usd_per_1m": 5.00,
            "cache_write_5m_usd_per_1m": 6.25,
            "cache_write_1h_usd_per_1m": 10.00,
            "cache_read_usd_per_1m": 0.50,
            "output_usd_per_1m": 25.00,
        },
        "claude-sonnet-4-5": {
            "input_usd_per_1m": 3.00,
            "cache_write_5m_usd_per_1m": 3.75,
            "cache_write_1h_usd_per_1m": 6.00,
            "cache_read_usd_per_1m": 0.30,
            "output_usd_per_1m": 15.00,
        },
        "claude-haiku-4-5-20251001": {
            "input_usd_per_1m": 1.00,
            "cache_write_5m_usd_per_1m": 1.25,
            "cache_write_1h_usd_per_1m": 2.00,
            "cache_read_usd_per_1m": 0.10,
            "output_usd_per_1m": 5.00,
        },
    }
    model_pricing['claude-opus-4-6'] = model_pricing['claude-opus-4-5']
    model_pricing['claude-sonnet-4-6'] = model_pricing['claude-sonnet-4-5']

    def get_model_pricing(self, model: str) -> dict[str, float]:
        """Get Claude pricing map for a model (exact match required)."""
        if model not in self.model_pricing:
            raise ValueError(
                f"No pricing configured for model {model!r} in provider {self.name!r}. "
                "Add it to model_pricing before running benchmarks."
            )
        pricing = self.model_pricing[model]
        if isinstance(pricing, dict):
            return cast(dict[str, float], pricing)
        raise ValueError(
            f"Invalid pricing configuration for model {model!r} in provider {self.name!r}."
        )

    def calculate_cost_with_cache(self, input_tokens: int, cache_creation_tokens: int, cache_read_tokens: int,
                                  output_tokens: int, model: str) -> float:
        """Estimate cost with explicit cache write/read rates."""
        pricing = self.get_model_pricing(model)
        base_input_tokens = max(0, input_tokens)
        cost = (
            base_input_tokens * pricing["input_usd_per_1m"]
            + cache_creation_tokens * pricing["cache_write_5m_usd_per_1m"]
            + cache_read_tokens * pricing["cache_read_usd_per_1m"]
            + output_tokens * pricing["output_usd_per_1m"]
        ) / 1_000_000
        return round(cost, 6)

    def build_test_command(self, model: str) -> list[str]:
        """Build a minimal test command to verify model availability."""
        return ["claude", "--print", "--model", model, "-p", "Reply with only the word OK"]

    def build_command(self, model: str, prompt: str, data_dir: str | None = None, permission_mode: str = "skip",
            workspace_dir: str = None, answer_output_path: str | None = None) -> list[str]:
        cmd = [
            "claude",
            "--print",
            "--verbose",  # Required for stream-json
            "--output-format", "stream-json",  # Captures full reasoning chain with tool calls
            "--model", model,
        ]

        # Only add workspace directory
        if workspace_dir:
            cmd.extend(["--add-dir", workspace_dir])

        # REMOVED: No longer add data_dir

        # Add permission flag based on mode
        if permission_mode == "skip":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(["--permission-mode", permission_mode])

        cmd.extend(["-p", prompt])
        return cmd

    def parse_output(self, stdout: str, model: str) -> tuple[str, dict]:
        """Parse Claude stream-json output (newline-delimited JSON events).

        Builds a rich trace showing the full reasoning chain including:
        - Assistant messages and thoughts
        - Tool calls (bash commands, file reads, etc.)
        - Tool results
        - Final result with usage stats

        Returns both reported_cost_usd (from Claude) and estimated_cost_usd (calculated).
        """
        usage = init_usage_dict(model, cache_creation_tokens=0, cache_read_tokens=0)
        trace_parts = []
        final_result = ""
        for event, line in parse_jsonl_events(stdout):
            event_type = event.get("type", "")

            if event_type == "raw":
                trace_parts.append(event["content"])

            elif event_type == "assistant":
                # Assistant message with content blocks
                message = event.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        msg = format_assistant_message("Claude", block.get("text", ""))
                        if msg:
                            trace_parts.append(msg)
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        # Format tool call
                        if tool_name == "Bash":
                            trace_parts.append(format_bash_tool(tool_input.get("command", "")))
                        elif tool_name in ("Read", "Write", "Edit"):
                            trace_parts.append(format_tool_call(tool_name, tool_input.get("file_path", "")))
                        elif tool_name == "Grep":
                            trace_parts.append(format_tool_call(tool_name, tool_input.get("pattern", "")))
                        elif tool_name in ("WebFetch", "WebSearch"):
                            trace_parts.append(format_tool_call(tool_name, tool_input.get("url", tool_input.get("query", ""))))
                        else:
                            trace_parts.append(format_tool_call(tool_name))

            elif event_type == "user":
                # Tool results
                message = event.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "tool_result":
                        content = block.get("content", "")
                        # Handle content as list of text blocks (e.g., from subagents)
                        if isinstance(content, list):
                            text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                            content = "\n".join(text_parts)
                        result = format_tool_result(content)
                        if result:
                            trace_parts.append(result)

            elif event_type == "result":
                # Final result with usage stats
                value = event.get("result", "")
                if isinstance(value, str):
                    final_result = value.strip()
                elif value is None:
                    final_result = ""
                else:
                    final_result = str(value).strip()
                usage["model"] = event.get("model", model)

                # Prefer per-model usage breakdown when present.
                if "modelUsage" in event and isinstance(event["modelUsage"], dict):
                    models = []
                    total_input = 0
                    total_output = 0
                    total_cache_creation = 0
                    total_cache_read = 0
                    for model_name, model_usage in event["modelUsage"].items():
                        if not isinstance(model_usage, dict):
                            continue
                        entry = {"model": model_name}
                        for key in (
                            "inputTokens",
                            "outputTokens",
                            "cacheReadInputTokens",
                            "cacheCreationInputTokens",
                            "webSearchRequests",
                            "costUSD",
                        ):
                            if key in model_usage:
                                entry[key] = model_usage[key]
                        models.append(entry)
                        total_input += int(model_usage.get("inputTokens", 0) or 0)
                        total_output += int(model_usage.get("outputTokens", 0) or 0)
                        total_cache_creation += int(model_usage.get("cacheCreationInputTokens", 0) or 0)
                        total_cache_read += int(model_usage.get("cacheReadInputTokens", 0) or 0)

                    usage["models"] = models
                    usage["input_tokens"] = total_input
                    usage["output_tokens"] = total_output
                    usage["cache_creation_tokens"] = total_cache_creation
                    usage["cache_read_tokens"] = total_cache_read
                elif "usage" in event:
                    u = event["usage"]
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)
                    usage["cache_creation_tokens"] = u.get("cache_creation_input_tokens", 0)
                    usage["cache_read_tokens"] = u.get("cache_read_input_tokens", 0)

                if "total_cost_usd" in event:
                    usage["reported_cost_usd"] = event["total_cost_usd"]
                    usage["cost_usd"] = event["total_cost_usd"]

        # Calculate estimated cost from model usage when available.
        if usage.get("models"):
            estimated_total = 0.0
            for model_usage in usage["models"]:
                model_name = str(model_usage.get("model", usage["model"]))
                estimated_total += self.calculate_cost_with_cache(
                    int(model_usage.get("inputTokens", 0) or 0),
                    int(model_usage.get("cacheCreationInputTokens", 0) or 0),
                    int(model_usage.get("cacheReadInputTokens", 0) or 0),
                    int(model_usage.get("outputTokens", 0) or 0),
                    model_name,
                )
            usage["estimated_cost_usd"] = round(estimated_total, 6)
        else:
            usage["estimated_cost_usd"] = self.calculate_cost_with_cache(
                usage["input_tokens"],
                usage["cache_creation_tokens"],
                usage["cache_read_tokens"],
                usage["output_tokens"],
                usage["model"],
            )
        if usage["cost_usd"] == 0.0:
            usage["cost_usd"] = usage["estimated_cost_usd"]

        # Build the full trace with final result at the end
        if final_result and final_result not in trace_parts:
            trace_parts.append(f"**Final Answer:**\n{final_result}")

        return build_trace_text(trace_parts, stdout), usage

    def extract_answer(self, stdout: str, model: str, answer_output_path: str | None = None) -> str:
        """Extract answer from Claude stream-json `result` events."""
        saw_result_event, final_result = get_last_claude_result_payload(stdout)

        # Claude may return multi-line text; benchmark answers are typically in the
        # final line, so take the last non-empty line from the result payload.
        answer = parse_answer(extract_last_non_empty_line(final_result))
        if answer.startswith("ERROR:") and not saw_result_event:
            return "ERROR: no result event"
        return answer


class CodexProvider(LLMProvider):
    """OpenAI Codex CLI provider.

    Model information and pricing from: https://openai.com/api/pricing/
    Last updated: 2026-02
    """

    name = "codex"
    default_model = "gpt-5.3-codex"
    conda_env = "codex-cli"
    install_url = "https://github.com/openai/codex"

    # Model pricing in USD per 1M tokens.
    model_pricing = {
        "gpt-5.4": {
            "input_usd_per_1m": 2.50,
            "cached_input_usd_per_1m": 0.25,
            "output_usd_per_1m": 15.00
        },
        "gpt-5.3-codex": {
            "input_usd_per_1m": 1.75,
            "cached_input_usd_per_1m": 0.175,
            "output_usd_per_1m": 14.00,
        },
        "gpt-5.1-codex-mini": {
            "input_usd_per_1m": 0.25,
            "cached_input_usd_per_1m": 0.025,
            "output_usd_per_1m": 2.00,
        },
    }

    def get_model_pricing(self, model: str) -> dict[str, float]:
        """Get Codex pricing map for a model."""
        if model in self.model_pricing:
            pricing = self.model_pricing[model]
            if isinstance(pricing, dict):
                return cast(dict[str, float], pricing)
        raise ValueError(
            f"No pricing configured for model {model!r} in provider {self.name!r}. "
            "Add it to model_pricing before running benchmarks."
        )

    def calculate_cost_with_cache(self, input_tokens: int, cached_input_tokens: int, output_tokens: int, model: str) -> float:
        """Calculate cost using separate rates for uncached input, cached input, and output."""
        pricing = self.get_model_pricing(model)
        uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
        cost = (
            uncached_input_tokens * pricing["input_usd_per_1m"]
            + cached_input_tokens * pricing["cached_input_usd_per_1m"]
            + output_tokens * pricing["output_usd_per_1m"]
        ) / 1_000_000
        return round(cost, 6)

    def build_test_command(self, model: str) -> list[str]:
        """Build a minimal test command to verify model availability."""
        return ["codex", "exec", "--model", model, "Reply with only the word OK"]

    def build_command(self, model: str, prompt: str, data_dir: str | None = None, permission_mode: str = "skip",
            workspace_dir: str = None, answer_output_path: str | None = None) -> list[str]:
        cmd = ["codex", "exec", "--json", "--model", model, "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        if answer_output_path:
            cmd.extend(["-o", answer_output_path])
        if workspace_dir:
            cmd.extend(["--add-dir", workspace_dir])
        # REMOVED: No longer add data_dir
        cmd.append(prompt)
        return cmd

    def parse_output(self, stdout: str, model: str) -> tuple[str, dict]:
        """Parse Codex JSONL output (newline-delimited JSON events).

        Builds a rich trace showing the full reasoning chain including:
        - Reasoning/thinking steps
        - Command executions (bash commands)
        - Web searches
        - Agent messages (final answers)

        Returns formatted markdown similar to Claude's trace format.
        """
        usage = init_usage_dict(model, cached_input_tokens=0)
        trace_parts = []

        for event, line in parse_jsonl_events(stdout):
            event_type = event.get("type", "")

            if event_type == "raw":
                trace_parts.append(event["content"])

            elif event_type == "item.completed":
                item = event.get("item", {})
                item_type = item.get("type", "")

                if item_type == "reasoning":
                    msg = format_assistant_message("Codex", item.get("text", ""), thinking=True)
                    if msg:
                        trace_parts.append(msg)

                elif item_type == "command_execution":
                    cmd = item.get("command", "")
                    output = item.get("aggregated_output", "")
                    if cmd:
                        trace_parts.append(format_bash_tool(cmd))
                    result = format_tool_result(output)
                    if result:
                        trace_parts.append(result)

                elif item_type == "web_search":
                    action = item.get("action", {})
                    action_type = action.get("type", "")
                    query = item.get("query", "") or action.get("query", "")
                    url = action.get("url", "")
                    if action_type == "search" and query:
                        trace_parts.append(format_tool_call("WebSearch", query))
                    elif action_type == "open_page" and url:
                        trace_parts.append(format_tool_call("WebFetch", url))

                elif item_type == "agent_message":
                    msg = format_assistant_message("Codex", item.get("text", ""))
                    if msg:
                        trace_parts.append(msg)

                elif item_type == "file_edit":
                    trace_parts.append(format_tool_call("Edit", item.get("path", "")))

                elif item_type == "file_read":
                    trace_parts.append(format_tool_call("Read", item.get("path", "")))

            elif event_type == "turn.completed":
                if "usage" in event:
                    u = event["usage"]
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)
                    usage["cached_input_tokens"] = u.get("cached_input_tokens", 0)

            elif event_type == "message.content":
                content = event.get("content", "")
                if content.strip():
                    trace_parts.append(content)

        usage["estimated_cost_usd"] = self.calculate_cost_with_cache(
            usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"], model
        )
        usage["cost_usd"] = usage["estimated_cost_usd"]
        usage["cached_tokens"] = usage["cached_input_tokens"]
        usage["models"] = [{
            "model": model,
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "output_tokens": usage["output_tokens"],
        }]

        trace_text = build_trace_text(trace_parts, stdout)
        return trace_text, usage

    def extract_answer(self, stdout: str, model: str, answer_output_path: str | None = None) -> str:
        """Extract answer from Codex `--output-last-message` file."""
        if not answer_output_path:
            return "ERROR: missing answer file path"
        return read_answer_file(answer_output_path)


class GeminiProvider(LLMProvider):
    """Google Gemini CLI provider.

    Model information and pricing from: https://ai.google.dev/gemini-api/docs/pricing
    Last updated: 2026-02
    Note: Some models have tiered pricing based on context length - using base rates here.
    """

    name = "gemini"
    default_model = "gemini-2.5-flash"
    conda_env = "gemini-cli"
    install_url = "https://github.com/google/gemini-cli"

    # Model pricing: (input_per_MTok, output_per_MTok)
    # Using base rates for standard context
    model_pricing = {
        # Gemini 3.x series (latest)
        "gemini-3.1-pro": (2.00, 12.00),     # gemini-3.1-pro-preview
        "gemini-3-pro": (2.00, 12.00),       # gemini-3-pro-preview
        "gemini-3-flash": (0.50, 3.00),      # gemini-3-flash-preview

        # Gemini 2.5 series
        "gemini-2.5-pro": (1.25, 10.00),
        "gemini-2.5-flash": (0.30, 2.50),
        "gemini-2.5-flash-lite": (0.10, 0.40),

        # Gemini 2.0 series
        "gemini-2.0-flash": (0.10, 0.40),
        "gemini-2.0-flash-lite": (0.075, 0.30),
    }

    def build_test_command(self, model: str) -> list[str]:
        """Build a minimal test command to verify model availability."""
        return ["gemini", "--model", model, "-p", "Reply with only the word OK"]

    def build_command(self, model: str, prompt: str, data_dir: str | None = None, permission_mode: str = "skip",
            workspace_dir: str = None, answer_output_path: str | None = None) -> list[str]:
        cmd = ["gemini", "--output-format", "stream-json", "--model", model, "--yolo", "--sandbox", "false"]
        if workspace_dir:
            cmd.extend(["--include-directories", workspace_dir])
        # REMOVED: No longer add data_dir
        cmd.extend(["-p", prompt])
        return cmd

    def parse_output(self, stdout: str, model: str) -> tuple[str, dict]:
        """Parse Gemini stream-json output (newline-delimited JSON events).

        Builds a rich trace showing the full reasoning chain including:
        - Tool calls (shell commands, web search, file operations)
        - Tool results
        - Assistant messages

        Returns formatted markdown similar to Claude's trace format.
        """
        usage = init_usage_dict(model, cached_tokens=0, thinking_tokens=0)
        trace_parts = []
        current_message = []  # Accumulate delta messages

        def flush_message():
            """Flush accumulated delta messages to trace."""
            if current_message:
                msg = format_assistant_message("Gemini", "".join(current_message))
                if msg:
                    trace_parts.append(msg)
                current_message.clear()

        for event, line in parse_jsonl_events(stdout):
            event_type = event.get("type", "")

            if event_type == "raw":
                trace_parts.append(event["content"])

            elif event_type == "tool_use":
                flush_message()
                tool_name = event.get("tool_name", "")
                params = event.get("parameters", {})

                if tool_name == "run_shell_command":
                    desc = params.get("description", "")
                    if desc:
                        trace_parts.append(format_assistant_message("Gemini", desc))
                    cmd = params.get("command", "")
                    if cmd:
                        trace_parts.append(format_bash_tool(cmd))
                elif tool_name == "google_web_search":
                    trace_parts.append(format_tool_call("WebSearch", params.get("query", "")))
                elif tool_name == "web_fetch":
                    trace_parts.append(format_tool_call("WebFetch", params.get("url", "")))
                elif tool_name in ("edit_file", "read_file", "write_file"):
                    tool_map = {"edit_file": "Edit", "read_file": "Read", "write_file": "Write"}
                    path = params.get("target_file", params.get("file_path", ""))
                    trace_parts.append(format_tool_call(tool_map[tool_name], path))
                else:
                    trace_parts.append(format_tool_call(tool_name))

            elif event_type == "tool_result":
                output = event.get("output", "")
                status = event.get("status", "")
                if output and output.strip():
                    trace_parts.append(format_tool_result(output))
                elif status == "error":
                    error = event.get("error", "Unknown error")
                    trace_parts.append(f"**Result:** Error - {error}")

            elif event_type == "message":
                role = event.get("role", "")
                content = event.get("content", "")
                is_delta = event.get("delta", False)

                if role == "assistant":
                    if is_delta:
                        current_message.append(content)
                    elif content.strip():
                        flush_message()
                        trace_parts.append(format_assistant_message("Gemini", content))

            elif event_type == "result":
                stats = event.get("stats", {})
                usage["input_tokens"] = stats.get("input_tokens", stats.get("input", 0))
                usage["output_tokens"] = stats.get("output_tokens", stats.get("output", 0))
                usage["cached_tokens"] = stats.get("cached", 0)

        # Flush any remaining message
        flush_message()

        # Calculate estimated cost (subtract cached tokens)
        effective_input = max(0, usage["input_tokens"] - usage["cached_tokens"])
        usage["estimated_cost_usd"] = self.calculate_cost(effective_input, usage["output_tokens"], usage["model"] or model)
        usage["cost_usd"] = usage["estimated_cost_usd"]

        trace_text = build_trace_text(trace_parts, stdout)
        return trace_text, usage

    def extract_answer(self, stdout: str, model: str, answer_output_path: str | None = None) -> str:
        """Gemini answer extraction is intentionally deferred."""
        raise NotImplementedError("answer extraction not implemented for gemini")


# Provider registry
LLM_PROVIDERS: dict[str, LLMProvider] = {
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
    "gemini": GeminiProvider(),
}

from wisp_provider import WispProvider, check_wisp_installed

LLM_PROVIDERS["wisp"] = WispProvider()


def get_provider(llm: str) -> LLMProvider:
    """Get LLM provider by name."""
    if llm not in LLM_PROVIDERS:
        raise ValueError(f"Unknown LLM: {llm}. Available: {list(LLM_PROVIDERS.keys())}")
    return LLM_PROVIDERS[llm]

DEFAULT_DATA_DIR = "/data/vibe-factory/data"


# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(run_dir: str, llm: str, model: str) -> logging.Logger:
    """Set up logging to both console and file."""
    logger = logging.getLogger(f"benchmark_{llm}_{model}")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    file_handler = logging.FileHandler(os.path.join(run_dir, "benchmark.log"), mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)

    return logger


# ============================================================================
# PROMPT GENERATION
# ============================================================================

def generate_prompt(question: str, file_paths: str | None, workspace_file_paths: list[str] | None, timeout_minutes: int = 60) -> str:
    """Generate a standardized prompt for all LLMs."""
    prompt_parts = [f"QUESTION: {question}"]

    # Update FILES section to use workspace paths
    if workspace_file_paths and len(workspace_file_paths) > 0:
        paths_str = ', '.join(workspace_file_paths)
        prompt_parts.extend([
            "",
            f"FILES: {paths_str}",
            "",
            "Note: All files are located in your current working directory (workspace)."
        ])

    prompt_parts.extend([
        "",
        "INSTRUCTIONS:",
        f"- You have {timeout_minutes} minutes to complete this task",
        "- Do not read/access any other files outside the workspace.",
        "- Get any files or tools you need from the internet.",
        "- You are free to modify the current conda environment as needed.",
        "- Keep all scripts and intermediate data in the workspace only.",
        "",
        "OUTPUT CONTRACT (IMPORTANT):",
        "- Your final response will be graded by exact string match.",
        "- Return EXACTLY ONE LINE containing **ONLY** the final answer in the format required by the question.",
        "- Do NOT include any explanation, reasoning, labels, prefixes, markdown, code fences, citations, or extra whitespace lines.",
        "- Any extra text before or after the answer is incorrect.",
        "- Before sending your final response, verify it is exactly one line and nothing else.", 
        ""
    ])
    return "\n".join(prompt_parts)


# ============================================================================
# LLM UTILITIES
# ============================================================================

def check_llm_installed(llm: str) -> tuple[bool, str, str]:
    """Check if an LLM CLI is installed. Returns (is_installed, message, version)."""
    if llm == "wisp":
        return check_wisp_installed()
    if llm not in LLM_PROVIDERS:
        return False, f"Unknown LLM: {llm}", ""
    provider = get_provider(llm)
    try:
        result = subprocess.run(["which", llm], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False, f"'{llm}' not found in PATH. Install: {provider.install_url}", ""
        cli_path = result.stdout.strip()
        result = subprocess.run([llm, "--version"], capture_output=True, text=True, timeout=10)
        version = result.stdout.strip().split('\n')[0] if result.stdout else "unknown"
        return True, f"'{llm}' at {cli_path}", version
    except Exception as e:
        return False, f"Error checking '{llm}': {e}", ""


def get_default_model(llm: str) -> str:
    """Get default model for an LLM provider."""
    return get_provider(llm).default_model


def get_conda_env(llm: str) -> str:
    """Get default conda environment for an LLM."""
    return get_provider(llm).conda_env


def get_cli_path(llm: str) -> str:
    """Get the full path to the CLI executable."""
    result = subprocess.run(["which", llm], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
        return result.stdout.strip()
    return llm  # Fall back to just the command name


def build_llm_command(llm: str, model: str, prompt: str, data_dir: str | None = None,
        permission_mode: str = "skip", workspace_dir: str = None,
        model_reasoning_effort: str | None = None, answer_output_path: str | None = None) -> list[str]:
    """Build command for LLM CLI execution with full CLI path."""
    cmd = get_provider(llm).build_command(
        model, prompt, data_dir, permission_mode, workspace_dir, answer_output_path
    )
    
    if llm == "claude" and cmd and cmd[0] == "claude":
        assert model_reasoning_effort is not None
        cmd = cmd[:1] + ["--effort", model_reasoning_effort] + cmd[1:]
    if llm == "codex" and len(cmd) >= 2 and cmd[0] == "codex" and cmd[1] == "exec":
        assert model_reasoning_effort is not None
        cmd = cmd[:2] + ["-c", f"model_reasoning_effort={model_reasoning_effort}"] + cmd[2:]

    # Replace the CLI name with the full path to make it work inside cloned conda envs
    cli_path = get_cli_path(llm)
    if cmd and cmd[0] == llm:
        cmd[0] = cli_path
    return cmd


# ============================================================================
# HELPERS
# ============================================================================

def safe_question_id(question_id: str) -> str:
    return question_id.replace('/', '_').replace(' ', '_')[:50]


def copy_files_to_workspace(file_paths: str | None, workspace_dir: str, logger: logging.Logger) -> list[str]:
    """Copy files/dirs to workspace root using only their basename.

    Args:
        file_paths: Comma-separated list of absolute paths to files/dirs
        workspace_dir: Target workspace directory
        logger: Logger instance for messages

    Returns:
        List of workspace-relative paths (e.g., ["file1.vcf", "genome/"])
    """
    if not file_paths:
        return []

    workspace_paths = []
    for path in file_paths.split(','):
        path = path.strip()
        if not path:
            continue
        if not os.path.exists(path):
            raise FileNotFoundError(f"Required file not found: {path}")

        name = os.path.basename(path)
        dest_path = os.path.join(workspace_dir, name)

        if os.path.isfile(path):
            shutil.copy2(path, dest_path)
            workspace_paths.append(name)
        elif os.path.isdir(path):
            shutil.copytree(path, dest_path, dirs_exist_ok=True)
            workspace_paths.append(name)

    return workspace_paths


def parse_answer(text: str) -> str:
    """Parse a final answer text by stripping surrounding whitespace."""
    if text is None:
        return "ERROR: empty answer"
    answer = str(text).strip()
    return answer if answer else "ERROR: empty answer"


def extract_last_non_empty_line(text: str | None) -> str:
    """Return the last non-empty stripped line from text, or empty string."""
    if text is None:
        return ""
    last_non_empty_line = ""
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped:
            last_non_empty_line = stripped
    return last_non_empty_line


def get_last_claude_result_payload(stdout: str) -> tuple[bool, str]:
    """Return (saw_result_event, last_result_payload) from Claude stream-json output."""
    saw_result_event = False
    final_result = ""
    for event, _ in parse_jsonl_events(stdout):
        if event.get("type") != "result":
            continue
        saw_result_event = True
        value = event.get("result", "")
        if isinstance(value, str):
            final_result = value
        elif value is None:
            final_result = ""
        else:
            final_result = str(value)
    return saw_result_event, final_result


def read_answer_file(answer_file: str) -> str:
    """Read answer text from a provider output file."""
    if not answer_file or not os.path.exists(answer_file):
        return "ERROR: missing answer file"
    try:
        with open(answer_file, encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"ERROR: failed to read answer file ({e})"
    return parse_answer(content)


def read_answer_file_raw(answer_file: str | None) -> str:
    """Read raw answer file content; return empty string if unavailable."""
    if not answer_file or not os.path.exists(answer_file):
        return ""
    try:
        with open(answer_file, encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ""


def validate_csv(df: pd.DataFrame, required_columns: list[str]) -> tuple[bool, str]:
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        return False, f"Missing columns: {', '.join(missing)}"
    return True, ""


# ============================================================================
# PROCESS EXECUTION
# ============================================================================

# Path to the compbio environment file
COMPBIO_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "environment.yml")

# Base environment name (created once, cloned for each question)
BASE_ENV_NAME = "compbio-benchmark"


def has_mamba() -> bool:
    """Check if mamba is available."""
    try:
        result = subprocess.run(["which", "mamba"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


# Cache mamba availability
_HAS_MAMBA: bool | None = None


def use_mamba() -> bool:
    """Check if we should use mamba (cached)."""
    global _HAS_MAMBA
    if _HAS_MAMBA is None:
        _HAS_MAMBA = has_mamba()
    return _HAS_MAMBA


def ensure_base_conda_env(logger: logging.Logger) -> bool:
    """Ensure the base compbio conda environment exists. Creates it from environment.yml if needed.

    Uses mamba for env creation if available (much faster dependency solving).
    """
    try:
        # Check if base env already exists
        result = subprocess.run(
            ["conda", "env", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            env_data = json.loads(result.stdout)
            env_names = [os.path.basename(e) for e in env_data.get("envs", [])]
            if BASE_ENV_NAME in env_names:
                logger.debug(f"Base environment '{BASE_ENV_NAME}' already exists")
                return True

        # Create base environment from file - try mamba first, fall back to conda
        if not os.path.exists(COMPBIO_ENV_FILE):
            logger.error(f"Environment file not found: {COMPBIO_ENV_FILE}")
            return False

        for cmd in (["mamba"] if use_mamba() else []) + ["conda"]:
            logger.info(f"Creating base environment '{BASE_ENV_NAME}' from {COMPBIO_ENV_FILE} (using {cmd})...")
            result = subprocess.run(
                [cmd, "env", "create", "-f", COMPBIO_ENV_FILE],
                capture_output=True,
                text=True,
                timeout=900  # 15 min for full env creation
            )
            if result.returncode == 0:
                break
            logger.warning(f"{cmd} failed: {result.stderr[:500]}")

        if result.returncode != 0:
            logger.error("Failed to create base conda env with all methods")
            return False

        logger.info(f"Base environment '{BASE_ENV_NAME}' created successfully")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout creating base conda environment")
        return False
    except Exception as e:
        logger.error(f"Error creating base conda env: {e}")
        return False


def clone_conda_env(clone_name: str, logger: logging.Logger) -> bool:
    """Clone the base compbio environment for a question.

    Always uses conda for cloning since mamba doesn't support --clone.
    Uses CONDA_NO_PLUGINS=true to avoid plugin conflicts in parallel execution.
    """
    try:
        logger.debug(f"Cloning '{BASE_ENV_NAME}' -> '{clone_name}'")
        env = os.environ.copy()
        env["CONDA_NO_PLUGINS"] = "true"
        result = subprocess.run(
            ["conda", "create", "-n", clone_name, "--clone", BASE_ENV_NAME, "-q", "-y"],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min for clone
            env=env
        )
        if result.returncode != 0:
            logger.error(f"Failed to clone conda env: {result.stderr[:500]}")
            return False
        logger.debug(f"Cloned environment: {clone_name}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout cloning conda env")
        return False
    except Exception as e:
        logger.error(f"Error cloning conda env: {e}")
        return False


def cleanup_conda_env(env_name: str, logger: logging.Logger) -> None:
    """Remove a conda environment."""
    try:
        logger.debug(f"Removing conda env: {env_name}")
        subprocess.run(
            ["conda", "env", "remove", "-n", env_name, "-y", "-q"],
            capture_output=True,
            timeout=60
        )
    except Exception as e:
        logger.warning(f"Failed to cleanup conda env {env_name}: {e}")


def conda_env_prefix(env_name: str) -> str | None:
    """Return the filesystem prefix of a named conda env, or None."""
    try:
        result = subprocess.run(
            ["conda", "env", "list", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        for path in json.loads(result.stdout).get("envs", []):
            if os.path.basename(path.rstrip(os.sep)) == env_name:
                return path
    except Exception:
        return None
    return None


class ProcessStartupHang(Exception):
    """Child produced no stdout/stderr before the startup silence limit."""

    def __init__(self, stdout: str, stderr: str, elapsed: float):
        super().__init__(f"no process output for {elapsed:.0f}s")
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed = elapsed


def _startup_silence_sec() -> int:
    raw = os.environ.get("BENCH_STARTUP_SILENCE_SEC", "180")
    try:
        return max(0, int(raw))
    except ValueError:
        return 180


def execute_llm_process(
    cmd: list[str],
    work_dir: str,
    timeout_seconds: int,
    question_id: str,
    logger: logging.Logger,
    conda_env: str | None = None,
    conda_run: bool = True,
) -> tuple[str, str, int | None, float]:
    """Execute LLM process with optional conda environment. Returns (stdout, stderr, return_code, elapsed_time)."""
    if is_shutdown_requested():
        raise InterruptedError("Shutdown requested")

    silence_limit = _startup_silence_sec()
    last_hang: ProcessStartupHang | None = None
    for attempt in (1, 2):
        try:
            return _execute_llm_process_once(
                cmd, work_dir, timeout_seconds, question_id, logger,
                conda_env, conda_run, silence_limit,
            )
        except ProcessStartupHang as hang:
            last_hang = hang
            if attempt == 2:
                raise
            logger.warning(
                f"[{question_id}] Startup hang after {hang.elapsed:.0f}s; retrying once"
            )
            shutil.rmtree(os.path.join(work_dir, ".wisp"), ignore_errors=True)
    raise last_hang  # pragma: no cover


def _execute_llm_process_once(
    cmd: list[str],
    work_dir: str,
    timeout_seconds: int,
    question_id: str,
    logger: logging.Logger,
    conda_env: str | None,
    conda_run: bool,
    silence_limit: int,
) -> tuple[str, str, int | None, float]:
    start_time = time.time()
    stdout_lines, stderr_lines = [], []
    launch_cmd = list(cmd)

    def read_stream(stream, lines_list, stream_name):
        try:
            for line in iter(stream.readline, ''):
                if line:
                    lines_list.append(line)
                    logger.debug(f"[{question_id}] [{stream_name}] {line.rstrip()}")
        except Exception as e:
            logger.error(f"[{question_id}] Error reading {stream_name}: {e}")
        finally:
            stream.close()

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    if conda_env:
        if conda_run:
            # --live-stream keeps system PATH (node/CLIs) visible to Claude/Codex/Gemini.
            launch_cmd = ["conda", "run", "-n", conda_env, "--live-stream"] + launch_cmd
            logger.debug(f"[{question_id}] Using conda run: {conda_env}")
        else:
            prefix = conda_env_prefix(conda_env)
            if not prefix:
                raise RuntimeError(f"conda env not found: {conda_env}")
            env["PATH"] = os.path.join(prefix, "bin") + os.pathsep + env.get("PATH", "")
            env["CONDA_PREFIX"] = prefix
            env["CONDA_DEFAULT_ENV"] = conda_env
            logger.debug(f"[{question_id}] Using conda PATH: {prefix}")

    process = subprocess.Popen(
        launch_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=work_dir, bufsize=1, env=env, start_new_session=True,
    )
    register_process(process)

    stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout_lines, "stdout"))
    stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr_lines, "stderr"))
    stdout_thread.start()
    stderr_thread.start()

    def _kill() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    try:
        deadline = start_time + timeout_seconds
        silence_deadline = start_time + silence_limit if silence_limit else None
        warned_silence = False
        while True:
            now = time.time()
            if now >= deadline:
                logger.warning(f"[{question_id}] Timeout after {timeout_seconds}s")
                _kill()
                exc = subprocess.TimeoutExpired(launch_cmd, timeout_seconds)
                exc.stdout = ''.join(stdout_lines).strip()
                exc.stderr = ''.join(stderr_lines).strip()
                raise exc
            if (
                silence_deadline
                and now >= silence_deadline
                and not stdout_lines
                and not stderr_lines
            ):
                elapsed = now - start_time
                logger.warning(f"[{question_id}] No output for {elapsed:.0f}s; killing hung startup")
                _kill()
                raise ProcessStartupHang("", "", elapsed)
            if (
                not warned_silence
                and not stdout_lines
                and not stderr_lines
                and now - start_time >= 60
            ):
                logger.warning(f"[{question_id}] Still no output after {now - start_time:.0f}s")
                warned_silence = True
            try:
                return_code = process.wait(timeout=min(5.0, deadline - now))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        unregister_process(process)

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    return (
        ''.join(stdout_lines).strip(),
        ''.join(stderr_lines).strip(),
        return_code,
        time.time() - start_time,
    )


# ============================================================================
# OUTPUT SAVING
# ============================================================================

def save_prompt_md(prompt_path: str, question_id: str, model: str, timeout_minutes: int,
                   difficulty: str, file_paths: str | None, workspace_paths: list[str] | None, prompt: str) -> None:
    """Save human-readable prompt file."""
    with open(prompt_path, 'w') as f:
        f.write(f"# Prompt: {question_id}\n\n")
        f.write("| Field | Value |\n")
        f.write("|-------|-------|\n")
        f.write(f"| **Model** | {model} |\n")
        f.write(f"| **Timeout** | {timeout_minutes} min |\n")
        f.write(f"| **Difficulty** | {difficulty} |\n")
        if file_paths:
            f.write(f"| **Original Files** | `{file_paths}` |\n")
        if workspace_paths:
            f.write(f"| **Workspace Files** | `{', '.join(workspace_paths)}` |\n")
        f.write(f"\n## Prompt\n\n```\n{prompt}\n```\n")


def save_trace_md(trace_path: str, question_id: str, llm: str, model: str,
                  timestamp: str, elapsed_time: float, return_code: int | None,
                  answer: str, result_text: str, usage: dict, stderr: str,
                  extraction_debug: dict[str, str] | None = None) -> None:
    """Save human-readable trace file with LLM reasoning and key metrics.

    This file is designed for human review - use result.json and raw_* files for machine parsing.
    """
    with open(trace_path, 'w') as f:
        f.write(f"# Trace: {question_id}\n\n")

        # Execution summary
        f.write("## Summary\n\n")
        f.write("| Field | Value |\n")
        f.write("|-------|-------|\n")
        f.write(f"| **LLM** | {llm} ({model}) |\n")
        f.write(f"| **Timestamp** | {timestamp} |\n")
        f.write(f"| **Elapsed** | {elapsed_time:.2f}s |\n")
        f.write(f"| **Return Code** | {return_code} |\n")

        # Usage metrics
        if usage:
            input_tok = usage.get('input_tokens', 0)
            output_tok = usage.get('output_tokens', 0)
            cost = usage.get('cost_usd', 0.0)
            f.write(f"| **Input Tokens** | {input_tok:,} |\n")
            f.write(f"| **Output Tokens** | {output_tok:,} |\n")
            # Show cache tokens if present (Claude)
            cache_creation = usage.get('cache_creation_tokens', 0)
            cache_read = usage.get('cache_read_tokens', 0)
            if cache_creation or cache_read:
                f.write(f"| **Cache Created** | {cache_creation:,} |\n")
                f.write(f"| **Cache Read** | {cache_read:,} |\n")
            # Show cached/thinking tokens if present (Gemini)
            cached = usage.get('cached_tokens', 0)
            thinking = usage.get('thinking_tokens', 0)
            if cached:
                f.write(f"| **Cached Tokens** | {cached:,} |\n")
            if thinking:
                f.write(f"| **Thinking Tokens** | {thinking:,} |\n")
            f.write(f"| **Cost** | ${cost:.4f} |\n")

        f.write("\n")

        # Final answer (extracted)
        f.write("## Answer\n\n")
        if answer:
            f.write(f"```\n{answer}\n```\n\n")
        else:
            f.write("_(no answer extracted)_\n\n")

        if extraction_debug:
            f.write("## Answer Extraction Debug\n\n")
            for key, value in extraction_debug.items():
                f.write(f"### {key}\n\n")
                if value:
                    f.write(f"```\n{value}\n```\n\n")
                else:
                    f.write("_(empty)_\n\n")

        # Full LLM reasoning/response
        f.write("## LLM Response\n\n")
        if result_text:
            f.write(f"{result_text}\n")
        else:
            f.write("_(no output)_\n")

        # Errors if any
        if stderr:
            f.write(f"\n## Errors\n\n```\n{stderr}\n```\n")


def save_raw_output_files(raw_stdout_path: str, raw_stderr_path: str, stdout: str, stderr: str) -> None:
    """Save raw provider stdout/stderr as verbatim files."""
    with open(raw_stdout_path, 'w', encoding='utf-8') as f:
        f.write(stdout or "")
    with open(raw_stderr_path, 'w', encoding='utf-8') as f:
        f.write(stderr or "")


def save_result_json(result_path: str, raw_stdout_path: str | None, raw_stderr_path: str | None,
                     idx: int, question_id: str, question: str,
                     file_paths: str | None, difficulty: str, prompt: str,
                     llm: str, model: str, version: str, timestamp: str,
                     return_code: int | None, answer: str, elapsed_time: float,
                     usage: dict, final_output: str, model_reasoning_effort: str | None = None) -> None:
    """Save inferred/normalized benchmark result JSON for a question."""

    execution = {
        "llm": llm,
        "model": model,
        "version": version,
        "timestamp": timestamp,
        "elapsed_time": round(elapsed_time, 2),
        "return_code": return_code,
    }
    if model_reasoning_effort:
        execution["model_reasoning_effort"] = model_reasoning_effort

    result = {
        "idx": idx,
        "question_id": question_id,
        "status": "error" if answer.startswith("ERROR") else "success",

        # Input
        "input": {
            "question": question,
            "file_paths": file_paths,
            "difficulty": difficulty,
            "prompt": prompt,
        },

        # Execution
        "execution": execution,

        # Output
        "output": {
            "answer": answer,
            "final_output": final_output,
            "raw_stdout_file": os.path.basename(raw_stdout_path) if raw_stdout_path else None,
            "raw_stderr_file": os.path.basename(raw_stderr_path) if raw_stderr_path else None,
        },

        # Usage & Cost
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "thinking_tokens": usage.get("thinking_tokens", 0),
            "models": usage.get("models", []),
        },
        "cost": {
            "reported_usd": usage.get("reported_cost_usd"),
            "estimated_usd": usage.get("estimated_cost_usd", 0.0),
            "total_usd": usage.get("cost_usd", 0.0),
        },
    }

    with open(result_path, 'w') as f:
        json.dump(result, f, indent=2)


# ============================================================================
# QUESTION RUNNER
# ============================================================================

def run_question(idx: int, row: pd.Series, llm: str, model: str, timeout_seconds: int,
                 run_dir: str, llm_version: str, date_run: str,
                 model_reasoning_effort: str | None, progress_state: dict, logger: logging.Logger,
                 keep_envs: bool = False, permission_mode: str = "skip",
                 reset_workspace: bool = False) -> tuple:
    """Run a single question through an LLM CLI."""
    # Check if shutdown was requested before starting
    if is_shutdown_requested():
        return idx, "ERROR: shutdown requested", 0.0

    question_id = row['question_id']
    question = row['question']
    file_paths = row.get('file_paths') if pd.notna(row.get('file_paths')) else None
    difficulty = str(row.get('difficulty', 'N/A'))

    timeout_minutes = timeout_seconds // 60
    safe_qid = safe_question_id(question_id)

    # Create question directory
    question_dir = os.path.join(run_dir, "questions", safe_qid)
    os.makedirs(question_dir, exist_ok=True)

    # File paths within question directory
    prompt_path = os.path.join(question_dir, "prompt.md")
    trace_path = os.path.join(question_dir, "trace.md")
    result_path = os.path.join(question_dir, "result.json")
    raw_stdout_path = os.path.join(question_dir, "raw_stdout.jsonl")
    raw_stderr_path = os.path.join(question_dir, "raw_stderr.txt")
    work_dir = os.path.join(question_dir, "workspace")
    answer_file = os.path.abspath(os.path.join(work_dir, "answer.txt")) if llm == "codex" else None
    if reset_workspace and os.path.exists(work_dir):
        logger.debug(f"[{question_id}] Resetting workspace for resume: {work_dir}")
        shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir, exist_ok=True)
    if answer_file and os.path.exists(answer_file):
        os.remove(answer_file)

    # Copy files to workspace BEFORE generating prompt
    workspace_file_paths = copy_files_to_workspace(file_paths, work_dir, logger)

    # Generate prompt with workspace-relative paths
    prompt = generate_prompt(question, file_paths, workspace_file_paths, timeout_minutes)

    # Save prompt
    save_prompt_md(prompt_path, question_id, model, timeout_minutes, difficulty, file_paths, workspace_file_paths, prompt)

    # Build command - NO LONGER passing data_dir
    cmd = build_llm_command(
        llm, model, prompt, None, permission_mode, work_dir,
        model_reasoning_effort=model_reasoning_effort,
        answer_output_path=answer_file
    )
    run_timestamp = datetime.now().isoformat()

    logger.debug(f"{'='*80}\nSTARTING: {question_id} | Index: {idx}\n{'='*80}")

    # Clone conda environment for this question (use UUID suffix for uniqueness in parallel runs)
    conda_env = f"benchmark_{llm}_{safe_qid}_{uuid.uuid4().hex[:8]}"
    # Register for cleanup on shutdown
    register_conda_env(conda_env)
    if not clone_conda_env(conda_env, logger):
        logger.error(f"[{question_id}] Failed to clone conda environment")
        # Record failure and return
        answer = "ERROR: conda clone failed"
        usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "model": model}
        save_trace_md(trace_path, question_id, llm, model, run_timestamp, 0.0, None,
                      answer, "", usage, "conda clone failed")
        save_raw_output_files(raw_stdout_path, raw_stderr_path, "", "conda clone failed")
        save_result_json(result_path, raw_stdout_path, raw_stderr_path,
                         idx, question_id, question, file_paths, difficulty, prompt,
                         llm, model, llm_version, run_timestamp, None, answer, 0.0, usage, "",
                         model_reasoning_effort=model_reasoning_effort)
        with progress_state['lock']:
            progress_state['completed'] += 1
            progress_state['errors'] += 1
            logger.info(f"[{progress_state['completed']:3d}/{progress_state['total']}] "
                        f"{question_id[:30]:<30} | D:{difficulty:<3} |     0s |    $0.00 | [ERR] conda clone failed")
        return idx, answer, 0.0

    stdout, stderr, return_code, elapsed_time = "", "", None, 0.0
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "model": model}
    result_text, answer = "", ""
    extraction_debug: dict[str, str] | None = None
    final_output = ""

    try:
        stdout, stderr, return_code, elapsed_time = execute_llm_process(
            cmd, work_dir, timeout_seconds, question_id, logger, conda_env,
            conda_run=(llm != "wisp"),
        )

        # Parse LLM output into trace + usage, then extract answer via provider-specific channel.
        provider = get_provider(llm)
        result_text, usage = provider.parse_output(stdout, model)
        answer = provider.extract_answer(stdout, model, answer_file)
        if llm == "claude":
            _, full_result_payload = get_last_claude_result_payload(stdout)
            final_output = full_result_payload
            extraction_debug = {
                "full_result_payload": full_result_payload,
                "extracted_last_non_empty_line": extract_last_non_empty_line(full_result_payload),
            }
        elif llm == "codex":
            final_output = read_answer_file_raw(answer_file)
        if answer.startswith("ERROR:"):
            logger.warning(f"[{question_id}] Answer extraction issue: {answer}")

    except ProcessStartupHang as e:
        elapsed_time, answer = e.elapsed, "ERROR: startup hang"
        stdout = e.stdout or ""
        stderr = (e.stderr or "") + f"\n\nSTARTUP HANG after {e.elapsed:.0f}s"
        result_text = stdout
        logger.warning(f"[{question_id}] {answer}")

    except subprocess.TimeoutExpired as e:
        elapsed_time, answer = float(timeout_seconds), "ERROR: timeout"
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        result_text = stdout  # Preserve any partial output
        stderr += f"\n\nTIMEOUT after {timeout_seconds}s"

    except ValueError as e:
        # Pricing config mismatches are fatal and should stop the whole run.
        if "No pricing configured for model" in str(e):
            logger.error(f"[{question_id}] Fatal pricing configuration error: {e}")
            raise
        answer = f"ERROR: {e}"
        result_text = stdout  # Preserve any partial output
        logger.exception(f"[{question_id}] ValueError: {e}")
        stderr += f"\n\nVALUE_ERROR: {e}"

    except NotImplementedError as e:
        answer = f"ERROR: {e}"
        logger.warning(f"[{question_id}] {answer}")
        stderr += f"\n\nNOT_IMPLEMENTED: {e}"

    except Exception as e:
        answer = f"ERROR: {e}"
        result_text = stdout  # Preserve any partial output
        logger.exception(f"[{question_id}] Exception: {e}")
        stderr += f"\n\nEXCEPTION: {e}"

    finally:
        # Cleanup cloned conda environment unless keeping for debugging
        if conda_env and not keep_envs:
            cleanup_conda_env(conda_env, logger)
        # Unregister from shutdown cleanup (already cleaned or keeping)
        unregister_conda_env(conda_env)

    # Save trace and result (always, even on error)
    save_trace_md(trace_path, question_id, llm, model, run_timestamp, elapsed_time, return_code,
                  answer, result_text, usage, stderr, extraction_debug=extraction_debug)
    save_raw_output_files(raw_stdout_path, raw_stderr_path, stdout, stderr)
    save_result_json(result_path, raw_stdout_path, raw_stderr_path,
                     idx, question_id, question, file_paths, difficulty, prompt,
                     llm, model, llm_version, run_timestamp, return_code, answer, elapsed_time, usage, final_output,
                     model_reasoning_effort=model_reasoning_effort)

    with progress_state['lock']:
        progress_state['completed'] += 1
        progress_state['total_cost'] = progress_state.get('total_cost', 0.0) + usage.get('cost_usd', 0.0)
        is_error = answer.startswith("ERROR")
        progress_state['errors' if is_error else 'successful'] += 1
        status = "ERR" if is_error else "DONE"
        cost_str = f"${usage.get('cost_usd', 0):.4f}"
        logger.info(f"[{progress_state['completed']:3d}/{progress_state['total']}] "
                    f"{question_id[:30]:<30} | D:{difficulty:<3} | {elapsed_time:5.0f}s | {cost_str:>8} | [{status}] {str(answer)[:25]}")

    return idx, answer, elapsed_time


# ============================================================================
# RESUME LOGIC
# ============================================================================


def get_questions_to_skip(run_dir: str) -> set[str]:
    """Find completed questions to skip when resuming.

    Returns a set of question_ids (not row indices) to handle CSV changes between runs.
    """
    skip = set()
    questions_dir = os.path.join(run_dir, "questions")
    if not os.path.exists(questions_dir):
        return skip
    for qid_dir in os.listdir(questions_dir):
        result_path = os.path.join(questions_dir, qid_dir, "result.json")
        if not os.path.exists(result_path):
            continue
        try:
            with open(result_path) as fp:
                data = json.load(fp)
            # Skip if completed successfully (not an error)
            # Use question_id instead of idx to handle CSV changes
            if data.get('question_id') and data.get('status') == 'success':
                skip.add(data['question_id'])
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return skip


# ============================================================================
# RUN COMMAND
# ============================================================================

# Local resource profile, not an official CompBioBench split. Bump the version
# when changing membership so resumed/merged runs keep the same task population.
BENCHMARK_PROFILE_VERSION = 1
FULL_ONLY_QUESTIONS = {
    "contaminated-rna-q1": "External taxonomic reference database; supplied FASTQ is already local",
    "contaminated-rna-q2": "External taxonomic reference database; supplied FASTQ is already local",
    "contaminated-rna-q3": "External taxonomic reference database; supplied FASTQ is already local",
    "encode-atac-pipeline-q1": "External ENCODE ATAC reference bundle and alignment indexes",
    "find-deletion-q1": "External hg38 genome and alignment index; supplied FASTQs are already local",
}


def select_questions(df: pd.DataFrame, profile: str, exclude=()) -> tuple[pd.DataFrame, dict[str, str]]:
    """Apply resource exclusions independently of model answers or elapsed time."""
    if profile not in ("default", "full"):
        raise ValueError(f"Unknown benchmark profile: {profile}")
    reasons = dict(FULL_ONLY_QUESTIONS) if profile == "default" else {}
    reasons.update({qid: "Excluded by --exclude" for qid in exclude})
    skipped = {qid: reasons[qid] for qid in df['question_id'] if qid in reasons}
    return df.loc[~df['question_id'].isin(skipped)].copy(), skipped


def cmd_run(args) -> None:
    """Run benchmark with a specific LLM.

    Supports multiple models via comma-separated list (e.g., -m opus-4-6,sonnet-4-5).
    Each model runs as a separate benchmark with its own output directory.
    """
    llm = args.llm
    keep_envs = getattr(args, 'keep_envs', False)
    resume = getattr(args, 'resume', None)
    resume_clean_workspace = getattr(args, 'resume_clean_workspace', False)
    model_reasoning_effort = getattr(args, 'model_reasoning_effort', None)

    # Parse model(s) - support comma-separated list
    model_arg = args.model or get_default_model(llm)
    models = [m.strip() for m in model_arg.split(',') if m.strip()]

    if len(models) > 1:
        print(f"Running {len(models)} models: {', '.join(models)}")
        for i, model in enumerate(models, 1):
            print(f"\n{'#'*80}\n# [{i}/{len(models)}] {llm}/{model}\n{'#'*80}\n")
            single_args = argparse.Namespace(
                llm=llm, model=model, input=args.input, parallel=args.parallel,
                timeout=args.timeout, results_dir=args.results_dir,
                resume=resume, keep_envs=keep_envs,
                resume_clean_workspace=resume_clean_workspace,
                model_reasoning_effort=model_reasoning_effort,
                reverse=getattr(args, 'reverse', False),
                exclude=getattr(args, 'exclude', []),
                profile=getattr(args, 'profile', None),
                list_questions=getattr(args, 'list_questions', False),
            )
            _run_single_model(single_args)
        return

    # Single model - run directly
    _run_single_model(args)


def _run_single_model(args) -> None:
    """Run benchmark with a single model (internal helper)."""
    llm = args.llm
    model = args.model or get_default_model(llm)
    provider = get_provider(llm)
    keep_envs = getattr(args, 'keep_envs', False)
    resume = getattr(args, 'resume', None)
    resume_clean_workspace = getattr(args, 'resume_clean_workspace', False)
    model_reasoning_effort = getattr(args, 'model_reasoning_effort', None)

    profile = getattr(args, 'profile', None)
    if resume:
        run_dir = os.path.join(args.results_dir, resume)
        metadata_path = os.path.join(run_dir, "run_metadata.json")
        if not os.path.isfile(metadata_path):
            raise ValueError(f"Run directory not found or invalid: {run_dir}")
        expected_prefix = f"{llm}_{model}_"
        if not resume.startswith(expected_prefix):
            raise ValueError(f"Resume directory must start with {expected_prefix}")
        with open(metadata_path, encoding='utf-8') as f:
            previous = json.load(f)
        # Runs created before profiles used the complete input CSV.
        previous_profile = previous.get("benchmark_profile", "full")
        if profile is not None and profile != previous_profile:
            raise ValueError(f"Cannot resume {previous_profile} as {profile}; start a new run")
        profile = previous_profile
        if profile == "default" and previous.get("benchmark_profile_version") != BENCHMARK_PROFILE_VERSION:
            raise ValueError("Default profile membership changed; start a new run")
    else:
        profile = profile or "default"
        run_dir = os.path.join(args.results_dir, f"{llm}_{model}_{profile}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

    df = pd.read_csv(args.input)
    valid, err = validate_csv(df, ['question_id', 'question', 'file_paths'])
    if not valid:
        raise ValueError(f"Invalid CSV: {err}")
    input_question_count = len(df)
    df, profile_skipped = select_questions(df, profile, getattr(args, 'exclude', []))
    label = "Benchmark-full" if profile == "full" else "Benchmark"
    print(f"{label}: {len(df)}/{input_question_count} questions selected; {len(profile_skipped)} skipped")
    for qid, reason in profile_skipped.items():
        print(f"  [SKIP] {qid}: {reason}")
    if getattr(args, 'list_questions', False):
        for qid in df['question_id']:
            print(f"  [SELECT] {qid}")
        return
    if df.empty:
        print("No questions selected.")
        return

    try:
        provider.get_model_pricing(model)
    except ValueError as e:
        print(f"ERROR: {e}")
        return

    print(f"Checking {llm} CLI...")
    is_installed, message, version = check_llm_installed(llm)
    if not is_installed:
        print(f"ERROR: {message}\nInstall: {provider.install_url}")
        return
    print(f"  {message} (v{version})")

    print(f"Testing model {model!r}...")
    model_ok, model_msg = provider.check_model_available(model, model_reasoning_effort=model_reasoning_effort)
    if not model_ok:
        print(f"ERROR: {model_msg}")
        return
    print(f"  {model_msg}")
    if llm in ("claude", "codex") and model_reasoning_effort:
        print(f"  {llm} model_reasoning_effort: {model_reasoning_effort}")

    # Ensure base conda environment exists (will be cloned for each question)
    if use_mamba():
        print("Using mamba for env creation (faster!), conda for cloning")
    else:
        print("Using conda for environment management")
    print("Checking base conda environment...")
    logger_init = logging.getLogger("benchmark_init")
    logger_init.setLevel(logging.INFO)
    if not ensure_base_conda_env(logger_init):
        print(f"ERROR: Failed to create base conda environment '{BASE_ENV_NAME}'")
        print("  Ensure environment.yml exists and conda is working")
        return
    print(f"  Base environment '{BASE_ENV_NAME}' ready (will clone for each question)")
    if keep_envs:
        print("  Keeping cloned environments after completion")
    if resume_clean_workspace and not resume:
        print("  NOTE: --resume-clean-workspace ignored without --resume")
        resume_clean_workspace = False

    if resume:
        print(f"Resuming: {run_dir}")

    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(os.path.join(run_dir, "questions"), exist_ok=True)
    logger = setup_logging(run_dir, llm, model)

    if resume:
        skip_qids = get_questions_to_skip(run_dir)
        questions: list[tuple[int, pd.Series]] = [(cast(int, i), r) for i, r in df.iterrows() if r['question_id'] not in skip_qids]
        logger.info(f"Resume: skipping {len(skip_qids)} completed")
    else:
        questions: list[tuple[int, pd.Series]] = [(cast(int, i), r) for i, r in df.iterrows()]

    if getattr(args, 'reverse', False):
        questions.reverse()

    if not questions:
        logger.info("All questions completed.")
        return

    logger.info("=" * 80)
    logger.info(f"{label} | Resource profile version: {BENCHMARK_PROFILE_VERSION} | Skipped: {len(profile_skipped)}")
    logger.info(f"LLM: {llm} | Model: {model} | Base env: {BASE_ENV_NAME}")
    logger.info(f"Questions: {len(questions)} | Parallel: {args.parallel} | Timeout: {args.timeout}min")
    logger.info("[DONE] means output returned; answer correctness is not graded by this runner.")
    logger.info("=" * 80)

    with open(os.path.join(run_dir, "run_metadata.json"), 'w') as f:
        json.dump({
            "llm": llm, "model": model, "version": version,
            "input_csv": args.input, "parallel_jobs": args.parallel,
            "timeout_minutes": args.timeout,
            "base_env": BASE_ENV_NAME, "keep_envs": keep_envs,
            "resume_clean_workspace": resume_clean_workspace,
            "model_reasoning_effort": model_reasoning_effort,
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "benchmark_profile": profile,
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "input_questions": input_question_count,
            "selected_question_ids": df['question_id'].tolist(),
            "skipped_questions": profile_skipped,
            "total_questions": len(df), "questions_to_run": len(questions),
        }, f, indent=2)

    date_run = datetime.now().strftime("%Y-%m-%d")
    progress = {'lock': threading.Lock(), 'completed': 0, 'total': len(questions), 'successful': 0, 'errors': 0}

    permission_mode = getattr(args, 'permission_mode', 'skip')
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(run_question, i, r, llm, model, args.timeout * 60, run_dir,
                            version, date_run, model_reasoning_effort, progress, logger,
                            keep_envs, permission_mode, resume and resume_clean_workspace): i
            for i, r in questions
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                idx = futures[future]
                logger.exception(f"Error at {idx}: {e}")
                if "No pricing configured for model" in str(e):
                    # Fatal config issue: stop all remaining work immediately.
                    request_shutdown()
                    cleanup_all_processes()
                    cleanup_all_conda_envs(logger)
                    raise
                # Update progress for unhandled exceptions
                with progress['lock']:
                    progress['completed'] += 1
                    progress['errors'] += 1

    logger.info("-" * 80)
    total_cost = progress.get('total_cost', 0.0)
    logger.info(f"Done! Outputs: {progress['successful']} | Errors: {progress['errors']} | Total Cost: ${total_cost:.4f}")
    if progress['errors'] > 0:
        logger.info(f"Retry: python run_benchmark.py run --llm {llm} --resume {os.path.basename(run_dir)}")


# ============================================================================
# RUN-ALL COMMAND
# ============================================================================

def cmd_run_all(args) -> None:
    """Run benchmark with all LLMs and merge."""
    if getattr(args, 'resume', None) and getattr(args, 'profile', None) is None:
        with open(os.path.join(args.results_dir, args.resume, "run_metadata.json"), encoding='utf-8') as f:
            args.profile = json.load(f).get("benchmark_profile", "full")
    llms = [(name, provider.default_model) for name, provider in LLM_PROVIDERS.items()]

    print("=" * 80)
    print(f"Running all LLMs: {', '.join(name for name, _ in llms)}")
    print("=" * 80)

    print("\nChecking CLIs...")
    available = []
    for llm, model in llms:
        ok, msg, ver = check_llm_installed(llm)
        print(f"  {'[OK]' if ok else '[ERR]'} {llm}: {msg}")
        if ok:
            available.append((llm, model))

    if not available:
        print("\nNo CLIs available.")
        return

    success, failed = [], []
    for i, (llm, model) in enumerate(available, 1):
        print(f"\n{'#'*80}\n# [{i}/{len(available)}] {llm}/{model}\n{'#'*80}\n")
        run_args = argparse.Namespace(
            llm=llm, model=model, input=args.input, parallel=args.parallel,
            timeout=args.timeout, results_dir=args.results_dir,
            resume=getattr(args, 'resume', None),
            keep_envs=getattr(args, 'keep_envs', False),
            resume_clean_workspace=getattr(args, 'resume_clean_workspace', False),
            model_reasoning_effort=getattr(args, 'model_reasoning_effort', None),
            reverse=getattr(args, 'reverse', False),
            exclude=getattr(args, 'exclude', []),
            profile=getattr(args, 'profile', None),
        )
        try:
            cmd_run(run_args)
            success.append((llm, model))
        except Exception as e:
            print(f"ERROR: {e}")
            failed.append((llm, model))

    print(f"\n{'#'*80}\n# Merging\n{'#'*80}\n")
    cmd_merge(argparse.Namespace(runs_dir=args.results_dir, input=args.input, output=args.output,
                               profile=getattr(args, 'profile', None)))
    print(f"\n{'='*80}\nDone! OK: {len(success)} | Failed: {len(failed)}\nOutput: {args.output}")


# ============================================================================
# MERGE COMMAND
# ============================================================================

def cmd_merge(args) -> None:
    """Merge results from multiple runs."""
    profile = getattr(args, 'profile', None) or "default"
    if not os.path.exists(args.runs_dir):
        print(f"No runs directory: {args.runs_dir}")
        return

    run_dirs = [os.path.join(args.runs_dir, d) for d in os.listdir(args.runs_dir)
                if os.path.isdir(os.path.join(args.runs_dir, d))
                and os.path.exists(os.path.join(args.runs_dir, d, "run_metadata.json"))]

    if not run_dirs:
        print(f"No runs found in {args.runs_dir}")
        return

    # Load metadata once so we can report input mismatches clearly.
    run_meta = []
    for run_dir in sorted(run_dirs):
        with open(os.path.join(run_dir, "run_metadata.json")) as f:
            meta = json.load(f)
        if meta.get("benchmark_profile", "full") != profile:
            print(f"Skipping other profile: {run_dir}")
            continue
        if profile == "default" and meta.get("benchmark_profile_version") != BENCHMARK_PROFILE_VERSION:
            print(f"Skipping different default profile version: {run_dir}")
            continue
        run_meta.append((run_dir, meta))

    if not run_meta:
        print(f"No runs found for profile {profile!r}.")
        return

    base_input_abs = os.path.abspath(args.input)
    meta_inputs = sorted({m.get("input_csv") for _, m in run_meta if m.get("input_csv")})
    if meta_inputs:
        meta_input_abs = sorted({os.path.abspath(p) for p in meta_inputs})
        if base_input_abs not in meta_input_abs:
            print("WARNING: merge base CSV does not match run metadata input CSV.")
            print(f"  merge --input: {args.input}")
            print(f"  run input_csv values: {', '.join(meta_inputs)}")
            if len(meta_inputs) == 1:
                print(f"  Hint: rerun merge with `-i {meta_inputs[0]}`")

    df = pd.read_csv(args.input)
    df, _ = select_questions(df, profile)
    print(f"Base: {args.input} ({len(df)} questions; profile: {profile})")

    # Track costs and question counts per run
    run_costs = {}
    run_question_counts = {}

    for run_dir, meta in run_meta:
        llm, model = meta['llm'], meta['model']
        print(f"  {llm}/{model}: {meta['timestamp']}")

        prefix = f"{llm}_{model}_{meta['timestamp']}"
        ans_col = f"answer_{prefix}"
        time_col = f"time_seconds_{prefix}"
        cost_col = f"cost_usd_{prefix}"
        input_tokens_col = f"input_tokens_{prefix}"
        output_tokens_col = f"output_tokens_{prefix}"
        df[ans_col], df[time_col], df[cost_col] = None, None, None
        df[input_tokens_col], df[output_tokens_col] = None, None

        # Initialize cost and question tracking for this run
        run_costs[prefix] = 0.0
        run_question_counts[prefix] = 0
        missing_count = 0
        total_result_files = 0

        questions_dir = os.path.join(run_dir, "questions")
        if not os.path.exists(questions_dir):
            continue
        for qid_dir in os.listdir(questions_dir):
            result_path = os.path.join(questions_dir, qid_dir, "result.json")
            if not os.path.exists(result_path):
                continue
            total_result_files += 1
            with open(result_path) as f:
                data = json.load(f)

            # Match by question_id, not idx (to handle CSV changes between runs)
            question_id = data['question_id']
            matching_rows = df[df['question_id'] == question_id]

            if len(matching_rows) == 0:
                print(f"  WARNING: Question '{question_id}' not found in base CSV, skipping")
                missing_count += 1
                continue

            idx = matching_rows.index[0]
            df.at[idx, ans_col] = data['output']['answer']
            df.at[idx, time_col] = data['execution']['elapsed_time']
            df.at[idx, cost_col] = data['cost']['total_usd']
            df.at[idx, input_tokens_col] = data['usage']['input_tokens']
            df.at[idx, output_tokens_col] = data['usage']['output_tokens']

            # Track total cost and question count for this run
            run_costs[prefix] += data['cost']['total_usd']
            run_question_counts[prefix] += 1

        if total_result_files > 0 and run_question_counts[prefix] == 0:
            run_input = meta.get('input_csv')
            msg = f"  WARNING: 0/{total_result_files} questions from {prefix} matched the base CSV."
            if run_input:
                msg += f" (run input_csv: {run_input})"
            print(msg)
        elif missing_count > 0:
            print(f"  WARNING: {missing_count}/{total_result_files} questions from {prefix} were missing in base CSV.")

    # Determine separator based on output file extension
    sep = '\t' if args.output.endswith('.tsv') else ','
    df.to_csv(args.output, index=False, sep=sep, quoting=csv.QUOTE_MINIMAL, escapechar='\\')
    print(f"Saved: {args.output}")

    # Print cost summary
    if run_costs:
        print("\n" + "=" * 80)
        print("COST SUMMARY")
        print("=" * 80)
        print(f"  {'Model':40} {'Questions':>10} {'Cost':>12}")
        print("-" * 80)
        total_cost = 0.0
        total_questions = 0
        for prefix in sorted(run_costs.keys()):
            cost = run_costs[prefix]
            count = run_question_counts[prefix]
            print(f"  {prefix:40} {count:10} ${cost:10.4f}")
            total_cost += cost
            total_questions += count
        print("-" * 80)
        print(f"  {'TOTAL':40} {total_questions:10} ${total_cost:10.4f}")
        print("=" * 80)


# ============================================================================
# PREPARE COMMAND
# ============================================================================

# Required columns in the xlsx file (must be present)
XLSX_REQUIRED_COLUMNS = ['question_id', 'question', 'file_paths']

# Optional columns in the xlsx file (used if present)
XLSX_OPTIONAL_COLUMNS = ['curator_name', 'domain', 'date_added', 'curator_difficulty_rating']

# Column mapping from xlsx column names to output CSV column names
XLSX_COLUMN_MAPPING = {
    'question_id': 'question_id',       # Required: Unique identifier for each question
    'question': 'question',             # Required: The question text to send to the LLM
    'file_paths': 'file_paths',         # Required: Comma-separated file paths for LLM access
    'curator_name': 'curator_name',     # Optional: Name of the person who created the question
    'domain': 'domain',                 # Optional: Domain category (e.g., Genomics, Transcriptomics)
    'date_added': 'date_added',         # Optional: Date the question was added
    'curator_difficulty_rating': 'difficulty',  # Optional: Difficulty rating (1-5)
}


def cmd_prepare(args) -> None:
    """Convert xlsx to benchmark CSV.

    The xlsx file must contain the following columns (row 2 is the header row):

    Required columns:
        - question_id: Unique identifier for each question
        - question: The question text to send to the LLM
        - file_paths: Comma-separated file paths for LLM access

    Optional columns:
        - curator_name: Name of the person who created the question
        - domain: Domain category (e.g., Genomics, Transcriptomics)
        - date_added: Date the question was added
        - curator_difficulty_rating: Difficulty rating (1-5), mapped to 'difficulty' in output
    """
    print(f"Preparing: {args.input} -> {args.output}")

    if not os.path.exists(args.input):
        print(f"ERROR: {args.input} not found")
        return

    try:
        df = pd.read_excel(args.input, sheet_name=args.sheet, header=1)
    except Exception as e:
        print(f"ERROR: {e}")
        return

    # Validate required columns are present
    missing_columns = [col for col in XLSX_REQUIRED_COLUMNS if col not in df.columns]
    if missing_columns:
        raise ValueError(
            f"Missing required columns in xlsx file: {missing_columns}. "
            f"Required columns are: {XLSX_REQUIRED_COLUMNS}. "
            f"Found columns: {list(df.columns)}"
        )

    cols = {k: v for k, v in XLSX_COLUMN_MAPPING.items() if k in df.columns}
    df_out = df[list(cols.keys())].rename(columns=cols)

    if 'question_id' in df_out.columns:
        df_out = df_out[df_out['question_id'].notna() & (df_out['question_id'] != '')]

        # Check for duplicate question_ids and make them unique
        duplicates = df_out[df_out.duplicated('question_id', keep=False)]
        if len(duplicates) > 0:
            print("\nWARNING: Duplicate question_ids found!")
            print("=" * 80)
            dup_ids = duplicates['question_id'].unique()
            rename_count = 0

            for qid in dup_ids:
                dup_mask = df_out['question_id'] == qid
                dup_indices = df_out[dup_mask].index.tolist()
                print(f"\n  question_id: {qid}")
                print(f"  Appears {len(dup_indices)} times at rows: {[i + 2 for i in dup_indices]}")

                # Keep first occurrence, add suffix to others
                for i, idx in enumerate(dup_indices[1:], start=2):
                    new_id = f"{qid}-dup{i}"
                    df_out.at[idx, 'question_id'] = new_id
                    print(f"    Renamed row {idx + 2}: {qid} -> {new_id}")
                    rename_count += 1

            print("\n" + "=" * 80)
            print(f"Made {rename_count} question_ids unique by adding suffixes")
    if 'question' in df_out.columns:
        df_out['question'] = df_out['question'].astype(str).str.strip().str.replace(r'\s+', ' ', regex=True)
        df_out = df_out[df_out['question'].notna() & (df_out['question'] != '') & (df_out['question'] != 'nan')]

    # Resolve file paths
    if 'file_paths' in df_out.columns:
        resolved = []
        for _, row in df_out.iterrows():
            fp = row.get('file_paths')
            if not fp or pd.isna(fp):
                resolved.append(None)
                continue
            paths = []
            for p in str(fp).split(','):
                p = p.strip()
                if not p:
                    continue
                if not p.startswith('/'):
                    p = os.path.join(args.data_dir, p)
                paths.append(os.path.normpath(p))
            resolved.append(', '.join(paths) if paths else None)
        df_out['file_paths'] = resolved

    # Verify all file paths exist on disk
    if not args.no_verify_files and 'file_paths' in df_out.columns:
        missing = []
        for _, row in df_out.iterrows():
            fp = row.get('file_paths')
            if not fp or pd.isna(fp):
                continue
            for p in str(fp).split(','):
                p = p.strip()
                if p and not os.path.exists(p):
                    missing.append(p)
        if missing:
            print(f"ERROR: {len(missing)} file(s) not found:")
            for p in missing:
                print(f"  {p}")
            sys.exit(1)

    df_out.to_csv(args.output, index=False)
    print(f"Saved {len(df_out)} questions")

    if not args.keep:
        os.remove(args.input)
        print(f"Deleted: {args.input}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="Benchmark Runner")
    subparsers = parser.add_subparsers(dest="command")

    # Prepare
    p = subparsers.add_parser("prepare", help="Convert xlsx to CSV")
    p.add_argument("-i", "--input", default="questions.xlsx")
    p.add_argument("-o", "--output", default="benchmark.csv")
    p.add_argument("-s", "--sheet", default="benchmark")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--no-verify-files", action="store_true", help="Skip checking that file_paths exist on disk")
    p.add_argument("--keep", action="store_true")

    # Run
    p = subparsers.add_parser("run", help="Run benchmark with one LLM")
    p.add_argument("--llm", choices=list(LLM_PROVIDERS.keys()), default="claude")
    p.add_argument("-m", "--model", help="Model name(s), comma-separated for multiple (e.g., opus-4-6,sonnet-4-5)")
    p.add_argument("-i", "--input", default="benchmark.csv")
    p.add_argument("-n", "--parallel", type=int, default=5)
    p.add_argument("-t", "--timeout", type=int, default=120, help="Timeout per question (minutes)")
    p.add_argument("--results-dir", default="benchmark_runs")
    p.add_argument("--resume", type=str, default=None, metavar="RUN_NAME",
                   help="Resume a specific run by folder name (e.g., claude_opus-4-6_20260329_120000)")
    p.add_argument("--resume-clean-workspace", action="store_true",
                   help="With --resume, remove each rerun question workspace before execution")
    p.add_argument("--keep-envs", action="store_true", help="Keep cloned conda envs after completion (for debugging)")
    p.add_argument("--model-reasoning-effort", default=None,
                   help="Reasoning effort (Claude: low|medium|high|max; Codex: minimal|low|medium|high|xhigh). Unused for wisp.")
    p.add_argument("--permission-mode", default="skip", choices=["default", "acceptEdits", "dontAsk", "skip"],
                   help="Permission mode: skip (bypass all, default), default (prompt), acceptEdits (auto-accept edits), dontAsk (auto-deny)")
    p.add_argument("--reverse", action="store_true", help="Run questions in reverse order")
    p.add_argument("--exclude", nargs="+", default=[], help="Question IDs to exclude (e.g., --exclude q1 q2 q3)")
    p.add_argument("--profile", choices=["default", "full"], default=None,
                   help="default: skip listed large external references; full: all input questions. Resume inherits the original profile.")
    p.add_argument("--list-questions", action="store_true",
                   help="List selected/skipped questions without starting a model or preparing environments")

    # Merge
    p = subparsers.add_parser("merge", help="Merge results")
    p.add_argument("--runs-dir", default="benchmark_runs")
    p.add_argument("-i", "--input", default="benchmark.csv")
    p.add_argument("-o", "--output", default="benchmark_results.csv")
    p.add_argument("--profile", choices=["default", "full"], default="default",
                   help="Merge only runs and questions from this resource profile (legacy runs are full)")

    # Run-all
    p = subparsers.add_parser("run-all", help="Run all LLMs and merge")
    p.add_argument("-i", "--input", default="benchmark.csv")
    p.add_argument("-o", "--output", default="benchmark_results.csv")
    p.add_argument("-n", "--parallel", type=int, default=5)
    p.add_argument("-t", "--timeout", type=int, default=120, help="Timeout per question (minutes)")
    p.add_argument("--results-dir", default="benchmark_runs")
    p.add_argument("--resume", type=str, default=None, metavar="RUN_NAME",
                   help="Resume a specific run by folder name (e.g., claude_opus-4-6_20260329_120000)")
    p.add_argument("--resume-clean-workspace", action="store_true",
                   help="With --resume, remove each rerun question workspace before execution")
    p.add_argument("--keep-envs", action="store_true", help="Keep cloned conda envs after completion")
    p.add_argument("--model-reasoning-effort", default=None,
                   help="Reasoning effort (Claude: low|medium|high|max; Codex: minimal|low|medium|high|xhigh). Unused for wisp.")
    p.add_argument("--permission-mode", default="skip", choices=["default", "acceptEdits", "dontAsk", "skip"],
                   help="Permission mode: skip (bypass all, default), default (prompt), acceptEdits (auto-accept edits), dontAsk (auto-deny)")
    p.add_argument("--reverse", action="store_true", help="Run questions in reverse order")
    p.add_argument("--exclude", nargs="+", default=[], help="Question IDs to exclude (e.g., --exclude q1 q2 q3)")
    p.add_argument("--profile", choices=["default", "full"], default=None,
                   help="default: skip listed large external references; full: all input questions. Resume inherits the original profile.")

    args = parser.parse_args()
    cmds = {"prepare": cmd_prepare, "run": cmd_run, "merge": cmd_merge, "run-all": cmd_run_all}
    if args.command in cmds:
        cmds[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
