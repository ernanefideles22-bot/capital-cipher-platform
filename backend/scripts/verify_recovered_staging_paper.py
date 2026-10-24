"""Native PostgreSQL PAPER smoke using an isolated database on the existing volume."""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, time, statistics
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid5, NAMESPACE_URL
import asyncpg

HOST = 'capital-cipher-recovered.railway.internal'
SOURCE_DB = 'capital_cipher_staging'
SMOKE_DB = 'capital_cipher_infra_smoke_20261023'
OWNER_MARKER = 'CC_INFRA_NATIVE_PAPER_SMOKE_V1'
PASS_MARKER = 'CC_INFRA_NATIVE_PAPER_SMOKE_V1_PASS'
CORRELATION = str(uuid5(NAMESPACE_URL, OWNER_MARKER))
DECISION_ID = str(uuid5(NAMESPACE_URL, OWNER_MARKER + ':decision'))

def require(condition, code):
    if not condition:
        raise RuntimeError(code)

def guards():
    require(os.getenv('APP_ENV') == 'staging', 'NOT_STAGING')
    require(os.getenv('SYSTEM_MODE') == 'PAPER', 'NOT_PAPER')
    require(os.getenv('OMS_EXECUTION_ENVIRONMENT') == 'PAPER', 'OMS_NOT_PAPER')
    require(os.getenv('OMS_TESTNET_ENABLED', '').lower() in {'false','0','off'}, 'TESTNET_NOT_FALSE')

async def phase(name):
    from app.database.session import Database
    from app.database.repositories.repository import Repository
    from app.audit.service import AuditService
    from app.core.state_machine import SystemStateMachine, SystemState
    from app.schemas.common import CandidateAction, PaperOrderStatus
    from app.schemas.decisions import Decision
    from app.schemas.risk import RiskLimits
    from app.risk.manager import RiskManager
    from app.paper_trading.engine import PaperTradingEngine
    db = Database(os.environ['DATABASE_URL'], pool_size=1, max_overflow=0)
    try:
        require(urlsplit(os.environ['DATABASE_URL']).hostname == HOST, 'WRONG_HOST')
        require(urlsplit(os.environ['DATABASE_URL']).path == '/' + SMOKE_DB, 'WRONG_SMOKE_DB')
        await db.verify_schema()
        repo = Repository(db)
        audit = AuditService(repository=repo)
        sm = SystemStateMachine()
        await sm.transition(SystemState.INITIALIZING, reason=OWNER_MARKER, actor='infra-validation')
        await sm.transition(SystemState.PAPER, reason=OWNER_MARKER, actor='infra-validation')
        risk = RiskManager(RiskLimits(), sm, audit, repository=repo)
        await risk.initialize()
        require(not risk.kill_switch_active, 'RECOVERED_KILL_SWITCH_ACTIVE')
        engine = PaperTradingEngine(audit, risk, repository=repo)
        await engine.initialize()
        orders = await repo.load_paper_orders()
        if name == 'open':
            require(not orders, 'SMOKE_ALREADY_HAS_ORDERS')
            decision = Decision(decision_id=DECISION_ID, correlation_id=CORRELATION,
                symbol='BTCUSDT', timeframe='15m', candidate_action=CandidateAction.BUY,
                confidence=80, reason=OWNER_MARKER,
                agent_summary=[{'name':'InfrastructureValidation','signal':'BUY','synthetic':True}])
            await repo.save_decision(decision)
            check = await risk.check(decision, entry_price=100.0, atr=1.0)
            require(check.approved, 'CENTRAL_RISK_NOT_APPROVED')
            order = await engine.create_order(decision, check, current_price=100.0)
            require(order.status == PaperOrderStatus.FILLED, 'ORDER_NOT_FILLED')
            persisted = await repo.load_paper_orders()
            require(len(persisted) == 1 and persisted[0].paper_order_id == order.paper_order_id, 'FILL_NOT_DURABLE')
        elif name == 'restore-close':
            require(len(orders) == 1 and len(engine.open_orders) == 1, 'OPEN_RESTART_RESTORE_FAILED')
            order = orders[0]
            require(order.correlation_id == CORRELATION, 'WRONG_SMOKE_CORRELATION')
            require(any(p.paper_order_id == order.paper_order_id for p in risk.position_exposures()), 'RISK_POSITION_NOT_RESTORED')
            closed = await engine.close_order(order.paper_order_id, order.take_profit, 'INFRA_PAPER_SMOKE')
            require(closed.status == PaperOrderStatus.CLOSED and closed.pnl is not None, 'CLOSE_FAILED')
        elif name == 'restore-closed':
            require(len(orders) == 1 and not engine.open_orders and len(engine.closed_orders) == 1, 'CLOSED_RESTART_RESTORE_FAILED')
            require(orders[0].status == PaperOrderStatus.CLOSED, 'ORDER_NOT_CLOSED')
            require(abs(engine.balance - (engine.initial_balance + orders[0].pnl)) < 1e-6, 'BALANCE_RESTORE_FAILED')
        else:
            raise RuntimeError('UNKNOWN_PHASE')
        print('CC_NATIVE_PAPER_PHASE_PASS ' + json.dumps({'phase':name,'database':SMOKE_DB,'mode':'PAPER','order_count':len(await repo.load_paper_orders()),'process_id':os.getpid()}, sort_keys=True), flush=True)
    finally:
        await db.engine.dispose()

async def main():
    guards()
    if len(sys.argv) > 1:
        return await phase(sys.argv[1])
    admin_url = os.environ['MIGRATION_DATABASE_URL']
    runtime_url = os.environ['DATABASE_URL']
    require(urlsplit(admin_url).hostname == HOST and urlsplit(admin_url).path == '/' + SOURCE_DB, 'ADMIN_TARGET_INVALID')
    require(urlsplit(runtime_url).hostname == HOST and urlsplit(runtime_url).username == 'capital_cipher_app', 'RUNTIME_TARGET_INVALID')
    measured = []
    runtime = await asyncpg.connect(runtime_url.replace('postgresql+asyncpg://','postgresql://',1))
    try:
        require(await runtime.fetchval('SELECT current_user') == 'capital_cipher_app', 'RUNTIME_ROLE_INVALID')
        safe_privileges = await runtime.fetchval("SELECT NOT has_schema_privilege(current_user,'capital_cipher','CREATE') AND bool_and(has_table_privilege(current_user,c.oid,'SELECT') AND NOT has_table_privilege(current_user,c.oid,'DELETE') AND NOT has_table_privilege(current_user,c.oid,'TRUNCATE')) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='capital_cipher' AND c.relkind='r'")
        require(safe_privileges, 'RUNTIME_PRIVILEGES_TOO_BROAD_OR_SELECT_MISSING')
        for _ in range(12):
            started = time.perf_counter()
            require(await runtime.fetchval('SELECT 1') == 1, 'RUNTIME_PING_FAILED')
            measured.append(round((time.perf_counter()-started)*1000, 3))
    finally:
        await runtime.close()
    latency = {'host':HOST,'samples':len(measured),'median_ms':round(statistics.median(measured),3),'max_ms':max(measured),'all_ms':measured}
    print('CC_PRIVATE_DATABASE_LATENCY ' + json.dumps(latency, sort_keys=True), flush=True)
    require(max(measured) < 1500, 'PRIVATE_DATABASE_LATENCY_EXCEEDS_1500MS')
    admin = await asyncpg.connect(admin_url)
    try:
        require(await admin.fetchval("SELECT current_setting('server_version_num')::int/10000") == 17, 'NATIVE17_REQUIRED')
        require(await admin.fetchval("SELECT count(*) FROM capital_cipher.paper_orders") == 0, 'SOURCE_PAPER_BASELINE_CHANGED')
        restrictions = dict(await admin.fetchrow("""SELECT
            (SELECT count(*) FROM pg_constraint WHERE connamespace IN ('capital_cipher'::regnamespace,'public'::regnamespace)) AS constraints,
            (SELECT count(*) FROM pg_constraint WHERE connamespace='capital_cipher'::regnamespace AND contype='f') AS foreign_keys,
            (SELECT count(*) FROM pg_constraint WHERE connamespace IN ('capital_cipher'::regnamespace,'public'::regnamespace) AND NOT convalidated) AS unvalidated_constraints,
            (SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('capital_cipher','public') AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped AND a.attnotnull) AS not_null_columns,
            (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='capital_cipher' AND NOT t.tgisinternal) AS application_triggers,
            (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='capital_cipher' AND NOT t.tgisinternal AND t.tgenabled='D') AS disabled_triggers,
            (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='capital_cipher' AND c.relkind='r' AND c.relrowsecurity) AS rls_tables,
            (SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('capital_cipher','public') AND NOT i.indisvalid) AS invalid_indexes
        """))
        require(restrictions == {'constraints':278,'foreign_keys':22,'unvalidated_constraints':0,'not_null_columns':560,'application_triggers':36,'disabled_triggers':0,'rls_tables':55,'invalid_indexes':0}, 'EXTENDED_RESTRICTIONS_DIFFER_FROM_BACKUP')
        print('CC_NATIVE17_EXTENDED_RESTRICTIONS_PASS ' + json.dumps(restrictions, sort_keys=True), flush=True)
    finally:
        await admin.close()
    control_url = urlunsplit(urlsplit(admin_url)._replace(path='/postgres'))
    admin = await asyncpg.connect(control_url)
    try:
        marker = await admin.fetchval("SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=$1", SMOKE_DB)
        exists = await admin.fetchval('SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)', SMOKE_DB)
        if exists:
            require(marker == PASS_MARKER, 'EXISTING_SMOKE_DB_REQUIRES_REVIEW')
            print('CC_NATIVE_PAPER_SMOKE_PREVIOUS_PASS', flush=True)
            return
        await admin.execute('CREATE DATABASE "' + SMOKE_DB + '" WITH TEMPLATE "' + SOURCE_DB + '"')
        await admin.execute('COMMENT ON DATABASE "' + SMOKE_DB + '" IS \'' + OWNER_MARKER + '\'')
    finally:
        await admin.close()
    child_env = dict(os.environ)
    child_env['DATABASE_URL'] = urlunsplit(urlsplit(runtime_url)._replace(path='/' + SMOKE_DB))
    for key in ('MIGRATION_DATABASE_URL','RUNTIME_DATABASE_PASSWORD','ADMIN_API_KEY'):
        child_env.pop(key, None)
    for step in ('open','restore-close','restore-closed'):
        result = subprocess.run([sys.executable, __file__, step], env=child_env, timeout=150)
        require(result.returncode == 0, 'PAPER_SUBPROCESS_FAILED:' + step)
    admin = await asyncpg.connect(admin_url)
    try:
        require(await admin.fetchval('SELECT count(*) FROM capital_cipher.paper_orders') == 0, 'SOURCE_PAPER_WAS_CHANGED')
        await admin.execute('COMMENT ON DATABASE "' + SMOKE_DB + '" IS \'' + PASS_MARKER + '\'')
    finally:
        await admin.close()
    print('CC_NATIVE_PAPER_CREATE_FILL_PROCESS_RESTART_CLOSE_BALANCE_PASS ' + json.dumps({'mode':'PAPER','database':SMOKE_DB,'source_database_changed':False,'subprocess_restarts':2,'testnet_enabled':False,'live_enabled':False,'new_services':0,'new_volumes':0}), flush=True)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        print('CC_NATIVE_PAPER_FAILED ' + type(exc).__name__ + ':' + (str(exc) if isinstance(exc, RuntimeError) else 'details withheld'), flush=True)
        raise SystemExit(1)
