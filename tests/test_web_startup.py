import subprocess
from pathlib import Path

import pytest

from db_agent import web_startup
from db_agent.cli import main
from db_agent.web_startup import WebStartupError, ensure_frontend


@pytest.fixture
def frontend(tmp_path, monkeypatch):
    root = tmp_path / "frontend"
    (root / "src").mkdir(parents=True)
    for name in ("package.json", "package-lock.json", "tsconfig.json"):
        (root / name).write_text("{}")
    (root / "index.html").write_text("<html></html>")
    (root / "src/main.tsx").write_text("initial source")
    calls = []

    def run(command, *, cwd, check):
        assert cwd == root and check is True
        calls.append(command[1:])
        if command[1:] == ["ci"]:
            (root / "node_modules/.bin").mkdir(parents=True, exist_ok=True)
            (root / "node_modules/.bin/vite").touch()
            (root / "node_modules/.package-lock.json").write_text("{}")
        else:
            (root / "dist").mkdir(exist_ok=True)
            (root / "dist/index.html").write_text("built page")

    monkeypatch.setattr(web_startup.shutil, "which", lambda name: "/fake/npm")
    monkeypatch.setattr(web_startup.subprocess, "run", run)
    return root, calls


def test_first_start_builds_and_unchanged_start_needs_no_npm(frontend, monkeypatch):
    root, calls = frontend
    assert ensure_frontend(root) == root / "dist"
    assert calls == [["ci"], ["run", "build"]]
    monkeypatch.setattr(web_startup.shutil, "which", lambda name: None)
    assert ensure_frontend(root) == root / "dist"
    assert len(calls) == 2


def test_existing_install_and_source_changes_rebuild_without_reinstall(frontend):
    root, calls = frontend
    (root / "node_modules/.bin").mkdir(parents=True)
    (root / "node_modules/.bin/vite").touch()
    (root / "node_modules/.package-lock.json").write_text("{}")
    ensure_frontend(root)
    (root / "src/main.tsx").write_text("updated source")
    ensure_frontend(root)
    (root / "src/main.tsx").unlink()
    ensure_frontend(root)
    assert calls == [["run", "build"]] * 3


@pytest.mark.parametrize("manifest", ["package.json", "package-lock.json"])
def test_changed_dependencies_reinstall_from_lock(frontend, manifest):
    root, calls = frontend
    ensure_frontend(root)
    (root / manifest).write_text('{"changed": true}')
    ensure_frontend(root)
    assert calls == [["ci"], ["run", "build"], ["ci"], ["run", "build"]]


def test_source_and_npm_requirements_have_actionable_errors(frontend, monkeypatch, tmp_path):
    root, _ = frontend
    with pytest.raises(WebStartupError, match="完整源码仓库"):
        ensure_frontend(tmp_path / "absent")
    monkeypatch.setattr(web_startup.shutil, "which", lambda name: None)
    with pytest.raises(WebStartupError, match="Node.js 和 npm"):
        ensure_frontend(root)


def test_build_failure_is_not_cached(frontend, monkeypatch):
    root, _ = frontend
    original = web_startup.subprocess.run

    def run(command, **kwargs):
        if command[1:] == ["run", "build"]:
            raise subprocess.CalledProcessError(1, command)
        original(command, **kwargs)

    monkeypatch.setattr(web_startup.subprocess, "run", run)
    with pytest.raises(WebStartupError, match="npm --prefix frontend run build"):
        ensure_frontend(root)
    assert not (root / "dist/.db-agent-build.sha256").exists()


@pytest.mark.parametrize("identity_path", [None, Path("custom-identities.json")])
def test_web_cli_defaults_to_local_mode_and_accepts_explicit_identities(
    monkeypatch, capsys, tmp_path, identity_path,
):
    import uvicorn

    from db_agent import web

    calls = []
    monkeypatch.setattr(web_startup, "ensure_frontend", lambda: tmp_path)
    monkeypatch.setattr(web, "create_app", lambda **kwargs: calls.append(kwargs) or "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    args = ["web", "--port", "8001"]
    if identity_path:
        args.extend(["--identity-file", str(identity_path)])
    assert main(args) == 0
    assert calls[0] == {"static_dir": tmp_path, "identity_path": identity_path}
    assert calls[1] == ("app", {"host": "127.0.0.1", "port": 8001, "access_log": False})
    assert "http://127.0.0.1:8001" in capsys.readouterr().out


def test_web_cli_does_not_start_when_build_fails(monkeypatch, capsys):
    import uvicorn

    def fail():
        raise WebStartupError("安装 Node.js 和 npm")

    monkeypatch.setattr(web_startup, "ensure_frontend", fail)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("must not start"))
    assert main(["web"]) == 2
    assert "安装 Node.js 和 npm" in capsys.readouterr().err
