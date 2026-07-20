"""Async database session management (docs/12)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base, INTERNAL_SCHEMA


class Database:
    def __init__(self, url: str) -> None:
        execution_options = None
        if url.startswith("sqlite"):
            # SQLite has no schemas. Keep the production schema boundary in
            # metadata while translating it to the default namespace in tests.
            execution_options = {
                "schema_translate_map": {INTERNAL_SCHEMA: None},
            }
        self.engine = create_async_engine(
            url,
            echo=False,
            execution_options=execution_options,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                await conn.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{INTERNAL_SCHEMA}"')
                )
                await conn.execute(
                    text(f'REVOKE ALL ON SCHEMA "{INTERNAL_SCHEMA}" FROM PUBLIC')
                )
            await conn.run_sync(Base.metadata.create_all)
            if self.engine.dialect.name == "postgresql":
                await conn.execute(
                    text(
                        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{INTERNAL_SCHEMA}" '
                        "FROM PUBLIC"
                    )
                )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()
