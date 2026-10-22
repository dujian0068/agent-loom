from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from agentloom_platform.cli import migrate
from agentloom_platform.config import Settings
from agentloom_platform.persistence import ChatSession, Database, Input, Run, RunEvent
from agentloom_platform.schemas import Draft


def test_upgrade_preserves_existing_runs_inputs_and_foreign_keys(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/legacy.db",
        data_dir=tmp_path,
        secret_key=Fernet.generate_key().decode(),
        _env_file=None,
    )
    root = Path(__file__).parents[1] / "api"
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    db = Database(settings.database_url)
    with db.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
    tables = sa.MetaData()
    tables.reflect(db.engine)
    with db.engine.begin() as connection:

        def insert(table, **values):
            connection.execute(tables.tables[table].insert().values(**values))

        insert("spaces", id="space", name="Existing space")
        insert("users", id="user", username="existing", password_hash="test-only", enabled=True)
        insert(
            "applications",
            id="app",
            space_id="space",
            name="Existing app",
            mode="react",
            revision=2,
            draft=Draft().model_dump(),
            release_id="release",
            lifecycle="active",
            created_at=1,
        )
        insert(
            "releases",
            id="release",
            app_id="app",
            version=1,
            draft_revision=2,
            snapshot={"draft": Draft().model_dump()},
            content_hash="test",
            created_at=1,
        )
        insert(
            "chat_sessions", id="session", app_id="app", owner_id="user", title="Existing chat", created_at=1
        )
        insert(
            "runs",
            id="run",
            session_id="session",
            release_id="release",
            thread_id="thread",
            status="completed",
            generation=1,
            host_id=None,
            result="Existing answer",
            error=None,
            cleanup_state="completed",
            model_calls=1,
            tool_calls=0,
            deadline_at=2,
            created_at=1,
            updated_at=2,
        )
        insert(
            "inputs",
            id="input",
            session_id="session",
            run_id="run",
            client_message_id="question",
            content="Existing question",
            status="accepted",
            created_at=1,
        )
        insert(
            "run_events", run_id="run", seq=1, kind="status", payload={"status": "completed"}, created_at=2
        )
    migrate(settings)
    migrate(settings)
    with db.session() as s:
        assert s.get(Run, "run").result == "Existing answer"
        assert s.get(Input, "input").content == "Existing question"
        assert s.get(ChatSession, "session").preview_snapshot is None
        assert s.get(RunEvent, ("run", 1)).payload == {"status": "completed"}
        assert s.execute(sa.text("PRAGMA foreign_key_check")).all() == []
        assert s.scalar(sa.text("PRAGMA foreign_keys")) == 1
    columns = {column["name"]: column for column in sa.inspect(db.engine).get_columns("runs")}
    assert columns["release_id"]["nullable"] is True
    db.engine.dispose()
