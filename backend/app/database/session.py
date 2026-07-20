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
            await self._install_central_risk_guards(conn)
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
                    "risk_evaluations",
                    "order_approvals",
                    "risk_control_state",
                    "risk_control_events",
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

    async def _install_central_risk_guards(self, conn) -> None:
        """Protect immutable evidence and approval lifecycle at the DB boundary."""

        immutable_tables = ("risk_evaluations", "risk_control_events")
        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_central_risk_evidence_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION 'central risk evidence is append-only'
                            USING ERRCODE = '55000';
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".guard_order_approval_transition()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        IF OLD.approval_id <> NEW.approval_id
                           OR OLD.evaluation_id <> NEW.evaluation_id
                           OR OLD.request_fingerprint <> NEW.request_fingerprint
                           OR OLD.position_snapshot_hash <>
                              NEW.position_snapshot_hash
                           OR OLD.decision_id <> NEW.decision_id
                           OR OLD.risk_check_id <> NEW.risk_check_id
                           OR OLD.symbol <> NEW.symbol
                           OR OLD.timeframe <> NEW.timeframe
                           OR OLD.strategy <> NEW.strategy
                           OR OLD.side <> NEW.side
                           OR OLD.max_notional <> NEW.max_notional
                           OR OLD.max_leverage <> NEW.max_leverage
                           OR OLD.reference_price <> NEW.reference_price
                           OR OLD.max_entry_deviation_bps <>
                              NEW.max_entry_deviation_bps
                           OR OLD.created_at <> NEW.created_at
                           OR OLD.expires_at <> NEW.expires_at THEN
                            RAISE EXCEPTION 'order approval identity is immutable'
                                USING ERRCODE = '55000';
                        END IF;
                        IF OLD.status <> 'ACTIVE'
                           OR NEW.status NOT IN ('CONSUMED', 'REVOKED', 'EXPIRED') THEN
                            RAISE EXCEPTION 'invalid order approval transition'
                                USING ERRCODE = '55000';
                        END IF;
                        RETURN NEW;
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".guard_risk_control_transition()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        IF OLD.singleton_id <> NEW.singleton_id
                           OR NEW.revision <> OLD.revision + 1
                           OR NEW.active = OLD.active THEN
                            RAISE EXCEPTION 'invalid risk control transition'
                                USING ERRCODE = '55000';
                        END IF;
                        RETURN NEW;
                    END;
                    $function$
                    """
                )
            )
            for function_name in (
                "reject_central_risk_evidence_mutation",
                "guard_order_approval_transition",
                "guard_risk_control_transition",
            ):
                await conn.execute(
                    text(
                        f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                        f'{function_name}() FROM PUBLIC'
                    )
                )
            for table_name in immutable_tables:
                trigger_name = f"trg_{table_name}_immutable"
                await conn.execute(
                    text(
                        f"""
                        DO $block$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1 FROM pg_trigger
                                WHERE tgname = '{trigger_name}'
                                  AND tgrelid =
                                    '{INTERNAL_SCHEMA}.{table_name}'::regclass
                            ) THEN
                                EXECUTE 'CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}"."{table_name}" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".reject_central_risk_evidence_mutation()';
                            END IF;
                        END;
                        $block$
                        """
                    )
                )
            await conn.execute(
                text(
                    f"""
                    DO $block$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_trigger
                            WHERE tgname = 'trg_order_approval_transition'
                              AND tgrelid =
                                '{INTERNAL_SCHEMA}.order_approvals'::regclass
                        ) THEN
                            EXECUTE 'CREATE TRIGGER trg_order_approval_transition BEFORE UPDATE ON "{INTERNAL_SCHEMA}"."order_approvals" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".guard_order_approval_transition()';
                        END IF;
                    END;
                    $block$
                    """
                )
            )
            await conn.execute(
                text(
                    f"""
                    DO $block$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_trigger
                            WHERE tgname = 'trg_risk_control_transition'
                              AND tgrelid =
                                '{INTERNAL_SCHEMA}.risk_control_state'::regclass
                        ) THEN
                            EXECUTE 'CREATE TRIGGER trg_risk_control_transition BEFORE UPDATE ON "{INTERNAL_SCHEMA}"."risk_control_state" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".guard_risk_control_transition()';
                        END IF;
                    END;
                    $block$
                    """
                )
            )
        elif self.engine.dialect.name == "sqlite":
            for table_name in immutable_tables:
                for operation in ("UPDATE", "DELETE"):
                    await conn.execute(
                        text(
                            f"""
                            CREATE TRIGGER IF NOT EXISTS
                            trg_{table_name}_immutable_{operation.lower()}
                            BEFORE {operation} ON {table_name}
                            BEGIN
                                SELECT RAISE(
                                    ABORT,
                                    'central risk evidence is append-only'
                                );
                            END
                            """
                        )
                    )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_order_approval_transition
                    BEFORE UPDATE ON order_approvals
                    WHEN OLD.status <> 'ACTIVE'
                      OR NEW.status NOT IN ('CONSUMED', 'REVOKED', 'EXPIRED')
                      OR OLD.approval_id <> NEW.approval_id
                      OR OLD.evaluation_id <> NEW.evaluation_id
                      OR OLD.request_fingerprint <> NEW.request_fingerprint
                      OR OLD.position_snapshot_hash <>
                         NEW.position_snapshot_hash
                      OR OLD.symbol <> NEW.symbol
                      OR OLD.timeframe <> NEW.timeframe
                      OR OLD.strategy <> NEW.strategy
                      OR OLD.side <> NEW.side
                      OR OLD.max_notional <> NEW.max_notional
                      OR OLD.max_leverage <> NEW.max_leverage
                      OR OLD.reference_price <> NEW.reference_price
                      OR OLD.max_entry_deviation_bps <>
                         NEW.max_entry_deviation_bps
                      OR OLD.created_at <> NEW.created_at
                      OR OLD.expires_at <> NEW.expires_at
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid order approval transition');
                    END
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_risk_control_transition
                    BEFORE UPDATE ON risk_control_state
                    WHEN NEW.singleton_id <> OLD.singleton_id
                      OR NEW.revision <> OLD.revision + 1
                      OR NEW.active = OLD.active
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid risk control transition');
                    END
                    """
                )
            )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()
