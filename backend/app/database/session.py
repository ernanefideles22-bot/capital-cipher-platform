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
            await self._install_walk_forward_immutability_guards(conn)
            await self._install_agent_evidence_immutability_guards(conn)
            if self.engine.dialect.name == "postgresql":
                await conn.execute(
                    text(
                        f'ALTER TABLE "{INTERNAL_SCHEMA}".'
                        '"walk_forward_experiments" '
                        "ENABLE ROW LEVEL SECURITY"
                    )
                )
                for table_name in (
                    "agent_execution_jobs",
                    "agent_execution_attempts",
                    "agent_memory_entries",
                ):
                    await conn.execute(
                        text(
                            f'ALTER TABLE "{INTERNAL_SCHEMA}".'
                            f'"{table_name}" ENABLE ROW LEVEL SECURITY'
                        )
                    )
                await conn.execute(
                    text(
                        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{INTERNAL_SCHEMA}" '
                        "FROM PUBLIC"
                    )
                )
                await conn.execute(
                    text(
                        f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA '
                        f'"{INTERNAL_SCHEMA}" FROM PUBLIC'
                    )
                )

    async def _install_walk_forward_immutability_guards(self, conn) -> None:
        """Reject UPDATE/DELETE at the database boundary for research artifacts."""

        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_walk_forward_experiment_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION
                            'walk_forward_experiments is append-only'
                            USING ERRCODE = '55000';
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                    "reject_walk_forward_experiment_mutation() FROM PUBLIC"
                )
            )
            await conn.execute(
                text(
                    f"""
                    DO $block$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1
                            FROM pg_trigger
                            WHERE tgname =
                                'trg_walk_forward_experiments_immutable'
                              AND tgrelid =
                                '{INTERNAL_SCHEMA}.walk_forward_experiments'
                                ::regclass
                        ) THEN
                            EXECUTE 'CREATE TRIGGER trg_walk_forward_experiments_immutable BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}"."walk_forward_experiments" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".reject_walk_forward_experiment_mutation()';
                        END IF;
                    END;
                    $block$
                    """
                )
            )
        elif self.engine.dialect.name == "sqlite":
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_walk_forward_experiments_immutable_update
                    BEFORE UPDATE ON walk_forward_experiments
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'walk_forward_experiments is append-only'
                        );
                    END
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_walk_forward_experiments_immutable_delete
                    BEFORE DELETE ON walk_forward_experiments
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'walk_forward_experiments is append-only'
                        );
                    END
                    """
                )
            )

    async def _install_agent_evidence_immutability_guards(self, conn) -> None:
        """Attempts and scoped memory are append-only evidence."""

        evidence_tables = (
            "agent_execution_attempts",
            "agent_memory_entries",
        )
        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_agent_evidence_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION
                            'agent runtime evidence is append-only'
                            USING ERRCODE = '55000';
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                    "reject_agent_evidence_mutation() FROM PUBLIC"
                )
            )
            for table_name in evidence_tables:
                trigger_name = f"trg_{table_name}_immutable"
                await conn.execute(
                    text(
                        f"""
                        DO $block$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1
                                FROM pg_trigger
                                WHERE tgname = '{trigger_name}'
                                  AND tgrelid =
                                    '{INTERNAL_SCHEMA}.{table_name}'::regclass
                            ) THEN
                                EXECUTE 'CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}"."{table_name}" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".reject_agent_evidence_mutation()';
                            END IF;
                        END;
                        $block$
                        """
                    )
                )
        elif self.engine.dialect.name == "sqlite":
            for table_name in evidence_tables:
                for operation in ("UPDATE", "DELETE"):
                    trigger_name = (
                        f"trg_{table_name}_immutable_{operation.lower()}"
                    )
                    await conn.execute(
                        text(
                            f"""
                            CREATE TRIGGER IF NOT EXISTS {trigger_name}
                            BEFORE {operation} ON {table_name}
                            BEGIN
                                SELECT RAISE(
                                    ABORT,
                                    'agent runtime evidence is append-only'
                                );
                            END
                            """
                        )
                    )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()
