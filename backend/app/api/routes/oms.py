"""Read-only OMS views plus authenticated cancel/reconcile controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/oms")


@router.get("/status")
async def oms_status(context: AppContext = Depends(get_context)) -> dict:
    latest = await _latest_reconciliation(context)
    return success_response(
        {
            "environment": context.oms_service.target_environment.value,
            "exchange": context.oms_service.target_exchange.value,
            "live_execution_available": False,
            "latest_reconciliation": (
                latest[0].model_dump(mode="json") if latest else None
            ),
        }
    )


@router.get("/orders")
async def list_oms_orders(
    limit: int = 200,
    context: AppContext = Depends(get_context),
) -> dict:
    orders = await context.oms_service.list_orders(
        limit=max(1, min(limit, 1_000))
    )
    return success_response(
        {"orders": [order.model_dump(mode="json") for order in orders]}
    )


@router.get("/orders/{oms_order_id}")
async def get_oms_order(
    oms_order_id: str,
    context: AppContext = Depends(get_context),
) -> dict:
    order = await context.oms_service.get_order(oms_order_id)
    if order is None:
        return error_response("NOT_FOUND", f"OMS order {oms_order_id} not found")
    fills = (
        await context.repository.load_execution_fills(
            oms_order_id=oms_order_id
        )
        if context.repository is not None
        else []
    )
    return success_response(
        {
            "order": order.model_dump(mode="json"),
            "fills": [fill.model_dump(mode="json") for fill in fills],
        }
    )


@router.post(
    "/orders/{oms_order_id}/cancel",
    dependencies=[AdminRequired],
)
async def cancel_oms_order(
    oms_order_id: str,
    context: AppContext = Depends(get_context),
) -> dict:
    order = await context.oms_service.queue_cancel(oms_order_id)
    return success_response({"order": order.model_dump(mode="json")})


@router.get("/reconciliation/latest")
async def latest_reconciliation(
    context: AppContext = Depends(get_context),
) -> dict:
    latest = await _latest_reconciliation(context)
    if latest is None:
        return success_response({"reconciliation": None})
    run, mismatches, positions, balances = latest
    return success_response(
        {
            "reconciliation": run.model_dump(mode="json"),
            "mismatches": [
                mismatch.model_dump(mode="json")
                for mismatch in mismatches
            ],
            "positions": [
                position.model_dump(mode="json") for position in positions
            ],
            "balances": [
                balance.model_dump(mode="json") for balance in balances
            ],
        }
    )


@router.post("/reconciliation/run", dependencies=[AdminRequired])
async def run_reconciliation(
    context: AppContext = Depends(get_context),
) -> dict:
    run = await context.reconciliation_service.reconcile_once()
    return success_response(
        {"reconciliation": run.model_dump(mode="json")}
    )


async def _latest_reconciliation(context: AppContext):
    if context.repository is not None:
        return await context.repository.load_latest_reconciliation(
            exchange=context.oms_service.target_exchange,
            environment=context.oms_service.target_environment,
        )
    latest = context.reconciliation_service.latest
    if latest is None:
        return None
    run, mismatches, snapshot = latest
    return run, mismatches, snapshot.positions, snapshot.balances
