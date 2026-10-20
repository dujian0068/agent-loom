import argparse
import getpass
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import select

from .config import Settings
from .persistence import Database, Membership, Space, User
from .resources import initialize_catalog
from .security import password_hash


def migrate(settings):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    from sqlalchemy import text

    db = Database(settings.database_url)
    with db.engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        if postgres:
            connection.execute(text("SELECT pg_advisory_lock(781205320)"))
            connection.commit()
        try:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        finally:
            if postgres:
                connection.execute(text("SELECT pg_advisory_unlock(781205320)"))
                connection.commit()
    db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="AgentLoom Platform")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--username", default="admin")
    initialize.add_argument("--space", default="团队空间")
    commands.add_parser("migrate")
    server = commands.add_parser("api")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", default=8000, type=int)
    commands.add_parser("openapi")
    args = parser.parse_args()
    if args.command == "init" and not os.getenv("PLATFORM_SECRET_KEY") and not Path(".env").exists():
        fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(f"PLATFORM_SECRET_KEY={Fernet.generate_key().decode()}\n")
    settings = Settings()
    settings.prepare()
    if args.command in {"init", "migrate"}:
        migrate(settings)
    if args.command == "init":
        password = os.getenv("PLATFORM_ADMIN_PASSWORD") or getpass.getpass("管理员密码（至少 12 字符）：")
        if len(password) < 12:
            raise SystemExit("Password must contain at least 12 characters")
        db = Database(settings.database_url)
        with db.session(write=True) as s:
            if s.scalar(select(User).where(User.username == args.username)):
                raise SystemExit("User already exists; no account was modified")
            user, space = (
                User(username=args.username, password_hash=password_hash(password)),
                Space(name=args.space),
            )
            s.add_all([user, space])
            s.flush()
            s.add(Membership(user_id=user.id, space_id=space.id, role="admin"))
            initialize_catalog(s, space.id)
        print(f"Initialized account {args.username} and space {args.space}")
    elif args.command == "api":
        import uvicorn

        from .http import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
    elif args.command == "openapi":
        from .http import create_app

        print(json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
