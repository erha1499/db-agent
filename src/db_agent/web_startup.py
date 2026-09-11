"""Prepare the source checkout's local Web assets without changing application configuration."""

import hashlib
import shutil
import subprocess
from pathlib import Path


class WebStartupError(RuntimeError):
    """A local build prerequisite or command failed."""


def _fingerprint(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _matches(path: Path, expected: str) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == expected
    except FileNotFoundError:
        return False


def ensure_frontend(frontend: Path | None = None) -> Path:
    """Install locked dependencies when needed, and reuse only the current source build."""
    frontend = frontend or Path(__file__).resolve().parents[2] / "frontend"
    manifests = [frontend / "package.json", frontend / "package-lock.json"]
    if not all(path.is_file() for path in [*manifests, frontend / "index.html"]):
        raise WebStartupError(
            "找不到网页源码。请在完整源码仓库运行 uv sync --locked 和 uv run db-agent web；"
            "单独安装的 Python wheel 不包含页面。"
        )
    try:
        source_paths = [
            *manifests, frontend / "index.html",
            *frontend.glob("tsconfig*.json"), *frontend.glob("vite.config.*"),
        ]
        for directory in ("src", "public"):
            source_paths.extend(
                path for path in (frontend / directory).rglob("*") if path.is_file()
            )
        source_hash = _fingerprint(frontend, source_paths)
        dist = frontend / "dist"
        build_marker = dist / ".db-agent-build.sha256"
        if (dist / "index.html").is_file() and _matches(build_marker, source_hash):
            return dist

        npm = shutil.which("npm")
        if npm is None:
            raise WebStartupError(
                "网页需要安装 Node.js 和 npm；安装后重新运行 uv run db-agent web。"
            )
        dependency_hash = _fingerprint(frontend, manifests)
        modules = frontend / "node_modules"
        dependency_marker = modules / ".db-agent-packages.sha256"
        installed_lock = modules / ".package-lock.json"
        if dependency_marker.is_file():
            dependencies_current = _matches(dependency_marker, dependency_hash)
        else:
            # A pre-existing npm ci installation can be reused on the first managed build.
            dependencies_current = installed_lock.is_file() and (
                installed_lock.stat().st_mtime_ns
                >= max(path.stat().st_mtime_ns for path in manifests)
            )
        if not dependencies_current or not (modules / ".bin/vite").is_file():
            print("正在按锁文件安装网页依赖（npm ci）…", flush=True)
            _run_npm(npm, ["ci"], frontend)
        dependency_marker.write_text(dependency_hash + "\n", encoding="utf-8")
        print("正在构建网页（npm run build）…", flush=True)
        _run_npm(npm, ["run", "build"], frontend)
        if not (dist / "index.html").is_file():
            raise WebStartupError(
                "网页构建未生成 dist/index.html；请执行 npm --prefix frontend run build。"
            )
        build_marker.write_text(source_hash + "\n", encoding="utf-8")
        return dist
    except OSError:
        raise WebStartupError(
            "无法读取或写入网页构建文件，请检查 frontend 的文件权限后重新运行 uv run db-agent web。"
        ) from None


def _run_npm(npm: str, args: list[str], frontend: Path) -> None:
    try:
        subprocess.run([npm, *args], cwd=frontend, check=True)
    except subprocess.CalledProcessError:
        command = " ".join(args)
        raise WebStartupError(
            f"网页准备失败。请按上方错误检查 Node.js、网络或源码，"
            f"执行 npm --prefix frontend {command} 修复后重新启动。"
        ) from None
