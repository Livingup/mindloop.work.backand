from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Spread AI desktop backend sidecar executable.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory where spread-ai-runtime.exe will be written.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    output_dir = args.output.resolve()
    build_dir = repo_root / "build" / "pyinstaller"
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        "--name",
        "spread-ai-runtime",
        "--distpath",
        str(output_dir),
        "--workpath",
        str(build_dir / "work"),
        "--specpath",
        str(build_dir / "spec"),
        "--collect-submodules",
        "agent_runtime",
        "--collect-data",
        "agent_runtime",
        "--collect-all",
        "psycopg",
        "--copy-metadata",
        "psycopg",
        str(repo_root / "src" / "agent_runtime" / "desktop_server.py"),
    ]
    subprocess.run(command, cwd=repo_root, check=True)


if __name__ == "__main__":
    main()
