"""Safety and isolation tests for the disposable migration validator."""

from __future__ import annotations

import pytest

from scripts import validate_supabase_migrations


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, *, schema_exists: bool = False) -> None:
        self.schema_exists = schema_exists
        self.executed: list[str] = []
        self.closed = False

    async def fetchval(self, statement: str) -> bool | int:
        if "to_regnamespace" in statement:
            return self.schema_exists
        if "information_schema.tables" in statement:
            return 1
        raise AssertionError(f"Unexpected query: {statement}")

    async def execute(self, statement: str) -> None:
        self.executed.append(statement)

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def close(self) -> None:
        self.closed = True


def _configure_disposable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "POSTGRES_MIGRATION_VALIDATION_URL",
        "postgresql://cipher:cipher@localhost:5432/disposable",
    )
    monkeypatch.setenv(
        "MIGRATION_VALIDATION_ACK",
        validate_supabase_migrations.ACKNOWLEDGEMENT,
    )


async def test_validator_removes_the_schema_it_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _configure_disposable_database(monkeypatch)

    async def connect(_database_url: str) -> _FakeConnection:
        return connection

    monkeypatch.setattr(validate_supabase_migrations.asyncpg, "connect", connect)

    await validate_supabase_migrations.main()

    assert connection.executed[-1] == (
        "drop schema if exists capital_cipher cascade"
    )
    assert connection.closed is True


async def test_validator_never_removes_a_preexisting_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(schema_exists=True)
    _configure_disposable_database(monkeypatch)

    async def connect(_database_url: str) -> _FakeConnection:
        return connection

    monkeypatch.setattr(validate_supabase_migrations.asyncpg, "connect", connect)

    with pytest.raises(
        RuntimeError,
        match="refuses a non-empty platform schema",
    ):
        await validate_supabase_migrations.main()

    assert not any(
        statement.startswith("drop schema")
        for statement in connection.executed
    )
    assert connection.closed is True
