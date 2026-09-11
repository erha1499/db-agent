"""Check distributable archives without extracting or printing their contents."""

import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def check() -> None:
    archives = sorted(Path("dist").glob("db_agent-*.whl"))
    sources = sorted(Path("dist").glob("db_agent-*.tar.gz"))
    if len(archives) != 1 or len(sources) != 1:
        raise ValueError("expected one wheel and one source distribution in dist")
    for archive in [*archives, *sources]:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as wheel:
                names = wheel.namelist()
        else:
            with tarfile.open(archive) as source:
                entries = source.getmembers()
                if any(item.issym() or item.islnk() or item.isdev() for item in entries):
                    raise ValueError("distribution contains a link or special file")
                names = [item.name for item in entries]
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("distribution contains an unsafe path")
            if any(
                part in {"work", "outputs", "artifacts", ".venv", "__pycache__", ".git"}
                or (part.startswith(".env") and part != ".env.example")
                or part.endswith((".pem", ".key", ".pyc"))
                for part in path.parts
            ):
                raise ValueError("distribution contains local configuration or runtime assets")
        if not any(name.endswith("db_agent/cli.py") for name in names):
            raise ValueError("distribution is missing the CLI module")
        print(f"{archive.name}: checked {len(names)} entries")


if __name__ == "__main__":
    check()
