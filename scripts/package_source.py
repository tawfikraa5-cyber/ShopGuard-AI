"""Build a source archive without runtime files or local data."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "app.py",
    "vision.py",
    "processing.py",
    "live.py",
    "tracker.yaml",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    ".prettierrc.json",
    ".gitignore",
    "README.md",
)
SOURCE_DIRS = ("static", "scripts", "tests")


def package(output):
    files = [ROOT / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        files.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in (".pyc", ".tmp")
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            archive.write(path, Path("ShopGuardAI") / path.relative_to(ROOT))

    with ZipFile(output) as archive:
        broken = archive.testzip()
        if broken:
            raise RuntimeError(f"Archive check failed: {broken}")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "ShopGuardAI-source.zip")
    args = parser.parse_args()
    count = package(args.output)
    print(f"{args.output}: {count} files")
