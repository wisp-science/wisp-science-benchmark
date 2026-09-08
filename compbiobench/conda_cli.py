"""Locate conda, mamba, or micromamba and issue environment commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class CondaNotFoundError(RuntimeError):
    pass


def _which(name: str) -> str | None:
    return shutil.which(name)


def _tool_name(cli: str) -> str:
    return Path(cli).name


def solver_cli() -> str:
    """CLI for env create/install. Prefers mamba-class solvers.

    If conda is present, stay on that install (mamba, else conda) so micromamba
    does not create a second, invisible prefix. Override with COMPBIO_CONDA_CMD.
    """
    override = os.environ.get("COMPBIO_CONDA_CMD", "").strip()
    if override:
        return override
    conda = _which("conda")
    mamba = _which("mamba")
    micromamba = _which("micromamba")
    if conda:
        return mamba or conda
    if micromamba:
        return micromamba
    if mamba:
        return mamba
    raise CondaNotFoundError("conda, mamba, or micromamba not found on PATH")


def manage_cli() -> str:
    """CLI for list/clone/remove/run. conda if present, else micromamba/mamba."""
    override = os.environ.get("COMPBIO_CONDA_CMD", "").strip()
    if override:
        return override
    conda = _which("conda")
    if conda:
        return conda
    micromamba = _which("micromamba")
    if micromamba:
        return micromamba
    mamba = _which("mamba")
    if mamba:
        return mamba
    raise CondaNotFoundError("conda, mamba, or micromamba not found on PATH")


def describe_driver() -> str:
    solver = _tool_name(solver_cli())
    manage = _tool_name(manage_cli())
    if solver == manage:
        return f"Using {solver} for environment management"
    return f"Using {solver} for env creation, {manage} for clone/list/remove"


def env_create_commands(env_file: str) -> list[list[str]]:
    cli = solver_cli()
    name = _tool_name(cli)
    if name in ("micromamba", "mamba"):
        return [
            [cli, "env", "create", "-f", env_file, "-y"],
            [cli, "create", "-f", env_file, "-y"],
        ]
    return [[cli, "env", "create", "-f", env_file]]


def env_remove_command(env_name: str) -> list[str]:
    cli = manage_cli()
    name = _tool_name(cli)
    if name == "conda":
        return [cli, "env", "remove", "-n", env_name, "-y", "-q"]
    return [cli, "env", "remove", "-n", env_name, "-y"]


def wrap_env_run(env_name: str, cmd: list[str]) -> list[str]:
    """Prefix cmd so it runs inside env_name. conda keeps --live-stream."""
    cli = manage_cli()
    name = _tool_name(cli)
    if name == "conda":
        return [cli, "run", "-n", env_name, "--live-stream", *cmd]
    return [cli, "run", "-n", env_name, *cmd]


def parse_env_paths(payload: object) -> list[str]:
    paths: list[str] = []
    if isinstance(payload, dict):
        raw = payload.get("envs")
        if raw is None:
            raw = payload.get("environments") or payload.get("env_list") or []
        for item in raw:
            if isinstance(item, str):
                paths.append(item)
            elif isinstance(item, dict):
                path = item.get("prefix") or item.get("path") or item.get("name")
                if path:
                    paths.append(str(path))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                paths.append(item)
            elif isinstance(item, dict):
                path = item.get("prefix") or item.get("path")
                if path:
                    paths.append(str(path))
    return paths


def list_env_paths() -> list[str]:
    cli = manage_cli()
    result = subprocess.run(
        [cli, "env", "list", "--json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return parse_env_paths(json.loads(result.stdout))
    except json.JSONDecodeError:
        return []


def env_prefix(env_name: str) -> str | None:
    for path in list_env_paths():
        if Path(path.rstrip(os.sep)).name == env_name:
            return path
    return None


def env_exists(env_name: str) -> bool:
    return env_prefix(env_name) is not None


def copy_env_prefix(src: str, dest: str) -> None:
    """Clone an env directory, hard-linking files when the filesystem allows."""
    src_path = Path(src)
    dest_path = Path(dest)
    if dest_path.exists():
        shutil.rmtree(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    def link_or_copy(source: str, target: str) -> None:
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    shutil.copytree(src_path, dest_path, symlinks=True, copy_function=link_or_copy)


def clone_command(src_name: str, dest_name: str) -> list[str]:
    cli = manage_cli()
    name = _tool_name(cli)
    if name == "conda":
        return [cli, "create", "-n", dest_name, "--clone", src_name, "-q", "-y"]
    return [cli, "create", "-n", dest_name, "--clone", src_name, "-y"]
