#!/usr/bin/env python3
"""Copy WispAdapter into a NatureBench clone and register it.

Does not change scoring. Official path: docs/custom-agents.md (Path B).

    python naturebench/install_adapter.py --naturebench-dir ~/benchmark/NatureBench
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMPORT_LINE = (
    "from . import wisp_adapter  # noqa: F401  (registers the Wisp adapter on import)\n"
)


def install(naturebench_dir: Path) -> None:
    agent_dir = naturebench_dir / "agent"
    init_py = agent_dir / "__init__.py"
    if not init_py.is_file():
        raise SystemExit(f"not a NatureBench clone (missing agent/__init__.py): {naturebench_dir}")

    dest = agent_dir / "wisp_adapter.py"
    shutil.copy2(HERE / "wisp_adapter.py", dest)
    print(f"copied {dest}")

    text = init_py.read_text(encoding="utf-8")
    if "wisp_adapter" in text:
        print(f"already registered in {init_py}")
        return
    if not text.endswith("\n"):
        text += "\n"
    init_py.write_text(text + IMPORT_LINE, encoding="utf-8")
    print(f"registered import in {init_py}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--naturebench-dir",
        type=Path,
        default=Path.home() / "benchmark" / "NatureBench",
        help="clone of github.com/FrontisAI/NatureBench",
    )
    args = parser.parse_args()
    install(args.naturebench_dir.resolve())


if __name__ == "__main__":
    main()
