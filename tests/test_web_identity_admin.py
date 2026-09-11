"""The local admin CLI changes only its named private identity file."""

import importlib.util
from pathlib import Path

import pytest

from db_agent.config import DatabaseSettings
from db_agent.web_identity import read_identities, verify_password


@pytest.fixture
def admin(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/manage_web_users.py"
    spec = importlib.util.spec_from_file_location("identity_admin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_database_settings", lambda: DatabaseSettings(
        _env_file=None, password="synthetic-db-password", allowed_tables=["orders", "customers"],
    ))
    target = tmp_path / "private/identities.json"

    def run(*args, passwords=("synthetic-new-password", "synthetic-new-password")):
        values = iter(passwords)
        monkeypatch.setattr(module.getpass, "getpass", lambda prompt: next(values))
        monkeypatch.setattr("sys.argv", ["manage_web_users.py", "--file", str(target), *args])
        return module.main()

    return run, target


def test_admin_set_replace_disable_and_list_without_password_output(admin, capsys):
    run, target = admin
    run("set", "alice", "--tables", "orders", "--allow-model", "--model-tables", "orders")
    first = read_identities(target, ("orders", "customers")).users[0]
    assert verify_password("synthetic-new-password", first.password_hash)
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    run("set", "alice", "--tables", "customers")
    replaced = read_identities(target, ("orders", "customers")).users[0]
    assert replaced.allowed_tables == ["customers"] and not replaced.model_enabled
    assert replaced.model_tables == [] and replaced.password_hash != first.password_hash
    run("list")
    output = capsys.readouterr()
    assert "synthetic-new-password" not in output.out + output.err
    assert "scrypt-v1" not in output.out + output.err
    run("disable", "alice")
    assert not read_identities(target, ("orders", "customers")).users[0].enabled


@pytest.mark.parametrize("failure", ["mismatch", "scope"])
def test_admin_rejected_update_keeps_original_private_file(admin, failure, capsys):
    run, target = admin
    run("set", "alice", "--tables", "orders")
    before = target.read_bytes()
    with pytest.raises(SystemExit) as exc:
        run("set", "alice", "--tables", "forbidden" if failure == "scope" else "orders",
            passwords=("synthetic-new-password", "different-password") if failure == "mismatch"
            else ("synthetic-new-password", "synthetic-new-password"))
    assert exc.value.code == 2
    assert target.read_bytes() == before
    assert not target.with_name(target.name + ".pending").exists()
    output = capsys.readouterr()
    assert "synthetic-new-password" not in output.out + output.err
