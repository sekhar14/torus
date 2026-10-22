"""Build a Claude Desktop Extension bundle (``dist/torus.mcpb``)."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from torus import __version__  # noqa: E402


def build_manifest() -> dict[str, Any]:
    """Return the MCPB manifest dictionary for Claude Desktop."""
    return {
        "manifest_version": "0.2",
        "name": "torus",
        "display_name": "Torus Research Memory",
        "version": __version__,
        "description": (
            "Persistent company, supply-chain and thesis memory for investment and"
            " corporate research."
        ),
        "author": {"name": "Torus Contributors"},
        "license": "Apache-2.0",
        "server": {
            "type": "python",
            "entry_point": "src/torus/__main__.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "torus", "serve"],
                "env": {
                    "TORUS_HARNESS": "claude-desktop",
                    "TORUS_SEC_CONTACT": "${user_config.sec_contact}",
                },
            },
        },
        "user_config": {
            "sec_contact": {
                "type": "string",
                "title": "SEC EDGAR Contact Email",
                "description": (
                    "Contact email sent in the SEC EDGAR User-Agent header when"
                    " downloading filings and reference tickers."
                ),
                "required": False,
            }
        },
    }


def build_mcpb(out_path: Path | None = None) -> Path:
    """Package Torus into a ``.mcpb`` zip archive and return its path."""
    target = out_path if out_path is not None else ROOT / "dist" / "torus.mcpb"
    target.parent.mkdir(parents=True, exist_ok=True)

    manifest_bytes = (json.dumps(build_manifest(), indent=2) + "\n").encode("utf-8")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        for top_file in ("pyproject.toml", "README.md", "LICENSE"):
            src_file = ROOT / top_file
            if src_file.exists():
                zf.write(src_file, arcname=top_file)
        src_root = ROOT / "src" / "torus"
        for path in sorted(src_root.rglob("*")):
            if path.is_dir():
                continue
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            arcname = path.relative_to(ROOT).as_posix()
            zf.write(path, arcname=arcname)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "dist" / "torus.mcpb",
        help="output .mcpb path (default: dist/torus.mcpb)",
    )
    args = parser.parse_args(argv)
    out = build_mcpb(args.out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
