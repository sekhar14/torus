"""Generate ``plugins/claude-code/skills`` and ``agents`` from ``torus.workflows``."""

from __future__ import annotations

from pathlib import Path

from torus.workflows import write_claude_code_plugin

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    plugin = ROOT / "plugins" / "claude-code"
    write_claude_code_plugin(plugin)
    print(f"Updated {plugin}")


if __name__ == "__main__":
    main()
