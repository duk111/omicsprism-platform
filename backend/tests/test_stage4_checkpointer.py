from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.agent import bootstrap
from scripts import migrate


ROOT = Path(__file__).resolve().parents[2]


class _Pool:
    instances: list["_Pool"] = []

    @staticmethod
    def check_connection(_conn) -> None:
        pass

    def __init__(self, *_args, **_kwargs) -> None:
        self.kwargs = _kwargs
        self.opened = False
        self.closed = False
        self.__class__.instances.append(self)

    def open(self, *, wait: bool) -> None:
        assert wait
        self.opened = True

    def close(self) -> None:
        self.closed = True


class _Saver:
    instances: list["_Saver"] = []

    def __init__(self, pool: _Pool) -> None:
        self.conn = pool
        self.__class__.instances.append(self)

    def setup(self) -> None:
        raise AssertionError("setup must be called only by the migration path")


def test_application_checkpointer_opens_a_pool_without_running_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Pool.instances.clear()
    _Saver.instances.clear()
    monkeypatch.setattr(bootstrap, "ConnectionPool", _Pool)
    monkeypatch.setattr(bootstrap, "PostgresSaver", _Saver)

    saver = bootstrap._create_postgres_checkpointer("postgresql://runtime")

    assert isinstance(saver, _Saver)
    assert _Pool.instances[0].opened
    assert _Pool.instances[0].kwargs["check"] is bootstrap.ConnectionPool.check_connection
    assert not _Pool.instances[0].closed
    saver.conn.close()
    assert _Pool.instances[0].closed


def test_agent_context_close_releases_checkpointer_pool() -> None:
    pool = _Pool()
    saver = _Saver(pool)

    class _Graph:
        checkpointer = saver

    context = bootstrap.AgentApiContext(
        product_store=object(),
        job_store=object(),
        graph=_Graph(),
        files=None,
    )

    context.close()

    assert pool.closed


def test_migration_checkpointer_setup_uses_admin_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class MigrationSaver:
        def __init__(self, pool: _Pool) -> None:
            calls.append("construct")

        def setup(self) -> None:
            calls.append("setup")

    monkeypatch.setattr(migrate, "ConnectionPool", _Pool, raising=False)
    monkeypatch.setattr(migrate, "PostgresSaver", MigrationSaver, raising=False)

    migrate.setup_agent_checkpointer("postgresql://admin")

    assert calls == ["construct", "setup"]
    assert _Pool.instances[-1].closed


def test_checkpoint_roles_grant_runtime_graph_access() -> None:
    sql = (ROOT / "migrations" / "011_agent_checkpoint_roles.sql").read_text(
        encoding="utf-8"
    ).lower()

    assert "grant select, insert, update, delete" in sql
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        assert table in sql
    assert "to omics_app" in sql
