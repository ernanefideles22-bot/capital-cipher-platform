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

    async def healthcheck(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                return bool(await connection.scalar(text("SELECT 1")))
        except Exception:
            return False

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
            await self._install_oms_guards(conn)
            await self._install_specialist_evaluation_guards(conn)
            await self._install_operational_evidence_guards(conn)
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
                    "oms_orders",
                    "oms_order_events",
                    "execution_commands",
                    "execution_fills",
                    "reconciliation_runs",
                    "reconciliation_mismatches",
                    "venue_position_snapshots",
                    "venue_balance_snapshots",
                    "specialist_evidence",
                    "agent_forecasts",
                    "agent_forecast_outcomes",
                    "consensus_experiments",
                    "consensus_experiment_events",
                    "weighted_consensus_snapshots",
                    "drift_observations",
                    "portfolio_proposals",
                    "operational_metric_snapshots",
                    "slo_evaluations",
                    "operational_alert_events",
                    "cost_usage_records",
                    "resilience_test_runs",
                    "shadow_campaign_checkpoints",
                    "shadow_validation_reports",
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

    async def _install_operational_evidence_guards(self, conn) -> None:
        """Keep operational and shadow-validation evidence append-only."""

        tables = (
            "operational_metric_snapshots",
            "slo_evaluations",
            "operational_alert_events",
            "cost_usage_records",
            "resilience_test_runs",
            "shadow_campaign_checkpoints",
            "shadow_validation_reports",
        )
        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_operational_evidence_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION
                            'operational evidence is append-only'
                            USING ERRCODE = '55000';
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                    "reject_operational_evidence_mutation() FROM PUBLIC"
                )
            )
            for table_name in tables:
                await conn.execute(
                    text(
                        f'DROP TRIGGER IF EXISTS trg_{table_name}_immutable '
                        f'ON "{INTERNAL_SCHEMA}"."{table_name}"'
                    )
                )
                await conn.execute(
                    text(
                        f'CREATE TRIGGER trg_{table_name}_immutable '
                        f'BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}".'
                        f'"{table_name}" FOR EACH ROW EXECUTE FUNCTION '
                        f'"{INTERNAL_SCHEMA}".'
                        "reject_operational_evidence_mutation()"
                    )
                )
            return
        if self.engine.dialect.name == "sqlite":
            for table_name in tables:
                await conn.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS
                        trg_{table_name}_immutable_update
                        BEFORE UPDATE ON {table_name}
                        BEGIN
                            SELECT RAISE(
                                ABORT,
                                'operational evidence is append-only'
                            );
                        END
                        """
                    )
                )
                await conn.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS
                        trg_{table_name}_immutable_delete
                        BEFORE DELETE ON {table_name}
                        BEGIN
                            SELECT RAISE(
                                ABORT,
                                'operational evidence is append-only'
                            );
                        END
                        """
                    )
                )

    async def _install_specialist_evaluation_guards(self, conn) -> None:
        """Keep evidence, forecasts and realized outcomes append-only."""

        tables = (
            "specialist_evidence",
            "agent_forecasts",
            "agent_forecast_outcomes",
            "consensus_experiments",
            "consensus_experiment_events",
            "weighted_consensus_snapshots",
            "drift_observations",
            "portfolio_proposals",
        )
        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_specialist_evaluation_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION
                            'specialist evaluation evidence is append-only'
                            USING ERRCODE = '55000';
                    END;
                    $function$
                    """
                )
            )
            await conn.execute(
                text(
                    f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                    "reject_specialist_evaluation_mutation() FROM PUBLIC"
                )
            )
            for table_name in tables:
                await conn.execute(
                    text(
                        f'DROP TRIGGER IF EXISTS trg_{table_name}_immutable '
                        f'ON "{INTERNAL_SCHEMA}"."{table_name}"'
                    )
                )
                await conn.execute(
                    text(
                        f'CREATE TRIGGER trg_{table_name}_immutable '
                        f'BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}".'
                        f'"{table_name}" FOR EACH ROW EXECUTE FUNCTION '
                        f'"{INTERNAL_SCHEMA}".'
                        "reject_specialist_evaluation_mutation()"
                    )
                )
            return
        if self.engine.dialect.name == "sqlite":
            for table_name in tables:
                await conn.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS
                        trg_{table_name}_immutable_update
                        BEFORE UPDATE ON {table_name}
                        BEGIN
                            SELECT RAISE(
                                ABORT,
                                'specialist evaluation evidence is append-only'
                            );
                        END
                        """
                    )
                )
                await conn.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS
                        trg_{table_name}_immutable_delete
                        BEFORE DELETE ON {table_name}
                        BEGIN
                            SELECT RAISE(
                                ABORT,
                                'specialist evaluation evidence is append-only'
                            );
                        END
                        """
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
                           OR OLD.correlation_id <> NEW.correlation_id
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

    async def _install_oms_guards(self, conn) -> None:
        """Protect OMS identity, command leases and append-only venue evidence."""

        evidence_tables = (
            "oms_order_events",
            "execution_fills",
            "reconciliation_runs",
            "reconciliation_mismatches",
            "venue_position_snapshots",
            "venue_balance_snapshots",
        )
        if self.engine.dialect.name == "postgresql":
            await conn.execute(
                text(
                    f"""
                    CREATE OR REPLACE FUNCTION
                    "{INTERNAL_SCHEMA}".reject_oms_evidence_mutation()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        RAISE EXCEPTION 'OMS evidence is append-only'
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
                    "{INTERNAL_SCHEMA}".guard_oms_order_transition()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        IF TG_OP = 'DELETE' THEN
                            RAISE EXCEPTION 'OMS orders cannot be deleted'
                                USING ERRCODE = '55000';
                        END IF;
                        IF OLD.oms_order_id IS DISTINCT FROM NEW.oms_order_id
                           OR OLD.client_order_id IS DISTINCT FROM
                              NEW.client_order_id
                           OR OLD.decision_id IS DISTINCT FROM NEW.decision_id
                           OR OLD.risk_check_id IS DISTINCT FROM NEW.risk_check_id
                           OR OLD.approval_id IS DISTINCT FROM NEW.approval_id
                           OR OLD.request_fingerprint IS DISTINCT FROM
                              NEW.request_fingerprint
                           OR OLD.correlation_id IS DISTINCT FROM
                              NEW.correlation_id
                           OR OLD.exchange IS DISTINCT FROM NEW.exchange
                           OR OLD.environment IS DISTINCT FROM NEW.environment
                           OR OLD.symbol IS DISTINCT FROM NEW.symbol
                           OR OLD.timeframe IS DISTINCT FROM NEW.timeframe
                           OR OLD.strategy IS DISTINCT FROM NEW.strategy
                           OR OLD.side IS DISTINCT FROM NEW.side
                           OR OLD.order_type IS DISTINCT FROM NEW.order_type
                           OR OLD.time_in_force IS DISTINCT FROM
                              NEW.time_in_force
                           OR OLD.quantity IS DISTINCT FROM NEW.quantity
                           OR OLD.requested_notional IS DISTINCT FROM
                              NEW.requested_notional
                           OR OLD.leverage IS DISTINCT FROM NEW.leverage
                           OR OLD.limit_price IS DISTINCT FROM NEW.limit_price
                           OR OLD.reference_price IS DISTINCT FROM
                              NEW.reference_price
                           OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                            RAISE EXCEPTION 'OMS order identity is immutable'
                                USING ERRCODE = '55000';
                        END IF;
                        IF NEW.state_version <> OLD.state_version + 1 THEN
                            RAISE EXCEPTION 'invalid OMS state version'
                                USING ERRCODE = '55000';
                        END IF;
                        IF OLD.status IN (
                            'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED',
                            'QUARANTINED'
                        ) OR NOT (
                            NEW.status = OLD.status
                            OR (
                                OLD.status IN (
                                    'CREATED', 'PENDING_SUBMISSION', 'SUBMITTED',
                                    'PARTIALLY_FILLED', 'CANCEL_PENDING',
                                    'UNKNOWN'
                                )
                                AND NEW.status IN (
                                    'PENDING_SUBMISSION', 'SUBMITTED',
                                    'PARTIALLY_FILLED', 'FILLED',
                                    'CANCEL_PENDING', 'CANCELED', 'REJECTED',
                                    'EXPIRED', 'UNKNOWN', 'QUARANTINED'
                                )
                            )
                        ) THEN
                            RAISE EXCEPTION 'invalid OMS status transition'
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
                    "{INTERNAL_SCHEMA}".guard_execution_command_transition()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    SECURITY INVOKER
                    SET search_path = ''
                    AS $function$
                    BEGIN
                        IF TG_OP = 'DELETE' THEN
                            RAISE EXCEPTION
                                'execution commands cannot be deleted'
                                USING ERRCODE = '55000';
                        END IF;
                        IF OLD.command_id IS DISTINCT FROM NEW.command_id
                           OR OLD.oms_order_id IS DISTINCT FROM NEW.oms_order_id
                           OR OLD.command_type IS DISTINCT FROM NEW.command_type
                           OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts
                           OR OLD.available_at IS DISTINCT FROM NEW.available_at
                           OR OLD.created_at IS DISTINCT FROM NEW.created_at
                           OR NOT (
                                (OLD.status = 'PENDING'
                                 AND NEW.status = 'LEASED')
                                OR (OLD.status = 'LEASED'
                                    AND NEW.status IN (
                                        'LEASED', 'COMPLETED', 'DEAD_LETTER'
                                    ))
                           ) THEN
                            RAISE EXCEPTION
                                'invalid execution command transition'
                                USING ERRCODE = '55000';
                        END IF;
                        RETURN NEW;
                    END;
                    $function$
                    """
                )
            )
            for function_name in (
                "reject_oms_evidence_mutation",
                "guard_oms_order_transition",
                "guard_execution_command_transition",
            ):
                await conn.execute(
                    text(
                        f'REVOKE ALL ON FUNCTION "{INTERNAL_SCHEMA}".'
                        f'{function_name}() FROM PUBLIC'
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
                                SELECT 1 FROM pg_trigger
                                WHERE tgname = '{trigger_name}'
                                  AND tgrelid =
                                    '{INTERNAL_SCHEMA}.{table_name}'::regclass
                            ) THEN
                                EXECUTE 'CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}"."{table_name}" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".reject_oms_evidence_mutation()';
                            END IF;
                        END;
                        $block$
                        """
                    )
                )
            for table_name, function_name in (
                ("oms_orders", "guard_oms_order_transition"),
                ("execution_commands", "guard_execution_command_transition"),
            ):
                trigger_name = f"trg_{table_name}_transition"
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
                                EXECUTE 'CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON "{INTERNAL_SCHEMA}"."{table_name}" FOR EACH ROW EXECUTE FUNCTION "{INTERNAL_SCHEMA}".{function_name}()';
                            END IF;
                        END;
                        $block$
                        """
                    )
                )
        elif self.engine.dialect.name == "sqlite":
            for table_name in evidence_tables:
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
                                    'OMS evidence is append-only'
                                );
                            END
                            """
                        )
                    )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS trg_oms_orders_no_delete
                    BEFORE DELETE ON oms_orders
                    BEGIN
                        SELECT RAISE(ABORT, 'OMS orders cannot be deleted');
                    END
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS trg_oms_orders_transition
                    BEFORE UPDATE ON oms_orders
                    WHEN OLD.oms_order_id IS NOT NEW.oms_order_id
                      OR OLD.client_order_id IS NOT NEW.client_order_id
                      OR OLD.decision_id IS NOT NEW.decision_id
                      OR OLD.risk_check_id IS NOT NEW.risk_check_id
                      OR OLD.approval_id IS NOT NEW.approval_id
                      OR OLD.request_fingerprint IS NOT NEW.request_fingerprint
                      OR OLD.correlation_id IS NOT NEW.correlation_id
                      OR OLD.exchange IS NOT NEW.exchange
                      OR OLD.environment IS NOT NEW.environment
                      OR OLD.symbol IS NOT NEW.symbol
                      OR OLD.timeframe IS NOT NEW.timeframe
                      OR OLD.strategy IS NOT NEW.strategy
                      OR OLD.side IS NOT NEW.side
                      OR OLD.order_type IS NOT NEW.order_type
                      OR OLD.time_in_force IS NOT NEW.time_in_force
                      OR OLD.quantity IS NOT NEW.quantity
                      OR OLD.requested_notional IS NOT NEW.requested_notional
                      OR OLD.leverage IS NOT NEW.leverage
                      OR OLD.limit_price IS NOT NEW.limit_price
                      OR OLD.reference_price IS NOT NEW.reference_price
                      OR OLD.created_at IS NOT NEW.created_at
                      OR NEW.state_version <> OLD.state_version + 1
                      OR OLD.status IN (
                          'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED',
                          'QUARANTINED'
                      )
                      OR NOT (
                          NEW.status = OLD.status
                          OR (
                              OLD.status IN (
                                  'CREATED', 'PENDING_SUBMISSION', 'SUBMITTED',
                                  'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN'
                              )
                              AND NEW.status IN (
                                  'PENDING_SUBMISSION', 'SUBMITTED',
                                  'PARTIALLY_FILLED', 'FILLED',
                                  'CANCEL_PENDING', 'CANCELED', 'REJECTED',
                                  'EXPIRED', 'UNKNOWN', 'QUARANTINED'
                              )
                          )
                      )
                    BEGIN
                        SELECT RAISE(ABORT, 'invalid OMS order transition');
                    END
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_execution_commands_no_delete
                    BEFORE DELETE ON execution_commands
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution commands cannot be deleted'
                        );
                    END
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TRIGGER IF NOT EXISTS
                    trg_execution_commands_transition
                    BEFORE UPDATE ON execution_commands
                    WHEN OLD.command_id IS NOT NEW.command_id
                      OR OLD.oms_order_id IS NOT NEW.oms_order_id
                      OR OLD.command_type IS NOT NEW.command_type
                      OR OLD.max_attempts IS NOT NEW.max_attempts
                      OR OLD.available_at IS NOT NEW.available_at
                      OR OLD.created_at IS NOT NEW.created_at
                      OR NOT (
                          (OLD.status = 'PENDING' AND NEW.status = 'LEASED')
                          OR (
                              OLD.status = 'LEASED'
                              AND NEW.status IN (
                                  'LEASED', 'COMPLETED', 'DEAD_LETTER'
                              )
                          )
                      )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'invalid execution command transition'
                        );
                    END
                    """
                )
            )

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def verify_testnet_oms_schema(self) -> None:
        """Fail TESTNET boot unless the Month 7 PostgreSQL boundary is ready."""

        if self.engine.dialect.name != "postgresql":
            raise RuntimeError("TESTNET OMS schema requires PostgreSQL")
        async with self.engine.connect() as conn:
            await conn.execute(
                text(
                    f'SELECT oms_order_id FROM "{INTERNAL_SCHEMA}".'
                    '"order_approvals" LIMIT 0'
                )
            )
            terminal_constraint = await conn.scalar(
                text(
                    "SELECT pg_get_constraintdef(c.oid) "
                    "FROM pg_constraint c "
                    "WHERE c.conname = 'ck_order_approval_terminal' "
                    "AND c.conrelid = "
                    f"'{INTERNAL_SCHEMA}.order_approvals'::regclass"
                )
            )
            if (
                terminal_constraint is None
                or "oms_order_id" not in terminal_constraint
            ):
                raise RuntimeError(
                    "Month 7 order approval migration is not applied"
                )
            rls_count = await conn.scalar(
                text(
                    "SELECT count(*) "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema_name "
                    "AND c.relname IN "
                    "('oms_orders', 'oms_order_events', "
                    "'execution_commands', 'execution_fills', "
                    "'reconciliation_runs', "
                    "'reconciliation_mismatches', "
                    "'venue_position_snapshots', "
                    "'venue_balance_snapshots') "
                    "AND c.relrowsecurity"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
            if rls_count != 8:
                raise RuntimeError(
                    "Month 7 OMS tables must have row-level security"
                )

    def session(self) -> AsyncSession:
        return self.session_factory()
