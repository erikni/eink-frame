"""Prepare a self-contained HA OS app from the shared renderer source."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT / "output" / "local-addon" / "eink_frame"


def main() -> None:
    """Copy app metadata and renderer, excluding disposable Python bytecode."""
    DESTINATION.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "Dockerfile", "entrypoint.py", "DOCS.md"):
        shutil.copy2(ROOT / "addon" / "eink_frame" / name, DESTINATION / name)
    shutil.copy2(ROOT / "requirements.txt", DESTINATION / "requirements.txt")
    shutil.copytree(
        ROOT / "renderer",
        DESTINATION / "renderer",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    print(DESTINATION)


if __name__ == "__main__":
    main()
