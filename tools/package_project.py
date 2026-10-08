"""Export a source link index and project archive without local credentials."""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
CORE_FILES = [
    "renderer/app.py",
    "pyproject.toml",
    ".github/workflows/python.yml",
    "renderer/__init__.py",
    "config.example.json",
    "firmware/src/main.cpp",
    "firmware/platformio.ini",
    "firmware/include/secrets.example.h",
    "addon/eink_frame/config.yaml",
    "addon/eink_frame/Dockerfile",
    "addon/eink_frame/entrypoint.py",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "compose.yaml",
    ".env.example",
    ".gitignore",
    ".dockerignore",
    "tools/package_addon.py",
    "tools/generate_previews.py",
    "tools/package_project.py",
    "tests/test_renderer.py",
]
DOCUMENTATION_FILES = [
    "README.md",
    "docs/zadani.md",
    "docs/mereni.md",
    "docs/protokol.md",
    "addon/eink_frame/DOCS.md",
]
PREVIEW_FILES = [
    "output/preview-future.png",
    "output/preview-active.png",
    "output/preview-empty.png",
]


def source_listing() -> str:
    """Link to the actual source files without duplicating their contents."""
    sections = ["# Zdrojové soubory e-ink rámečku\n"]
    sections.extend(f"- [{filename}](../{filename})" for filename in CORE_FILES)
    return "\n".join(sections) + "\n"


def main() -> None:
    """Write source documentation and archive only explicitly approved files."""
    OUTPUT.mkdir(exist_ok=True)
    listing_path = OUTPUT / "cely-kod.md"
    listing_path.write_text(source_listing(), encoding="utf-8")
    filenames = (
        CORE_FILES + DOCUMENTATION_FILES + PREVIEW_FILES + ["output/cely-kod.md"]
    )
    with zipfile.ZipFile(
        OUTPUT / "eink-frame-komplet.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for filename in filenames:
            archive.write(ROOT / filename, "eink-frame/" + filename)
    print(listing_path)
    print(OUTPUT / "eink-frame-komplet.zip")


if __name__ == "__main__":
    main()
