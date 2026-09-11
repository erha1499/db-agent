"""Operator-only local identity management. Never registered as an Agent tool."""

import argparse
import getpass
import json
import os
from pathlib import Path

from db_agent.config import ConfigurationError, load_database_settings
from db_agent.web_identity import Identity, IdentityFile, password_hash, read_identities


def main():
    parser = argparse.ArgumentParser(description="本机 Web 身份管理；不修改数据库账号权限")
    parser.add_argument("--file", type=Path, default=Path("outputs/web/identities.json"))
    parser.add_argument("operation", choices=["set", "disable", "list"])
    parser.add_argument("username", nargs="?")
    parser.add_argument("--display-name")
    parser.add_argument("--tables", nargs="*", default=[])
    parser.add_argument("--model-tables", nargs="*", default=[])
    parser.add_argument("--allow-model", action="store_true")
    args = parser.parse_args()
    try:
        database = load_database_settings()
        exists = args.file.exists()
        data = read_identities(args.file, database.allowed_tables) if exists else None
        if args.operation == "list":
            if not data:
                raise ValueError("身份文件不存在。")
            print(json.dumps([user.model_dump(exclude={"password_hash"}) for user in data.users],
                             ensure_ascii=False, indent=2))
            return
        if not args.username:
            raise ValueError("请提供用户名。")
        users = list(data.users) if data else []
        existing = next((user for user in users if user.username == args.username), None)
        if args.operation == "disable":
            if not existing:
                raise ValueError("用户不存在。")
            replacement = existing.model_copy(update={"enabled": False})
        else:
            password = getpass.getpass("新密码（12–128字符，不显示）：")
            if password != getpass.getpass("再输入一次："):
                raise ValueError("两次密码不一致。")
            replacement = Identity(
                username=args.username, display_name=args.display_name or args.username,
                password_hash=password_hash(password), allowed_tables=args.tables,
                model_enabled=args.allow_model, model_tables=args.model_tables,
            )
        users = [user for user in users if user.username != args.username] + [replacement]
        payload = IdentityFile(version=1, users=users).model_dump_json(indent=2)
        args.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = args.file.with_name(args.file.name + ".pending")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            read_identities(temporary, database.allowed_tables)
            os.replace(temporary, args.file)
        finally:
            temporary.unlink(missing_ok=True)
        print("身份配置已保存。正在运行的服务会撤销旧登录；重新登录使用新授权范围。")
    except (ConfigurationError, ValueError, OSError):
        parser.exit(2, "身份管理失败：请检查输入、私有文件权限和数据库白名单；未输出凭据。\n")


if __name__ == "__main__":
    main()
