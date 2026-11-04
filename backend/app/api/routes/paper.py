"""Paper trading endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/paper")


@router.get("/orders")
async def list_orders(context: AppContext = Depends(get_context)) -> dict:
    engine = context.paper_engine
    visible = [*engine.open_orders.values(), *engine.closed_orders[-100:]]
    return success_response({
        "orders": [o.model_dump(mode="json") for o in visible],
        "cost_reporting": engine.cost_reporting(),
        "order_costs": [engine.order_cost_report(o) for o in visible],
    })


@router.get("/orders/{order_id}")
async def get_order(order_id: str, context: AppContext = Depends(get_context)) -> dict:
    engine = context.paper_engine
    order = engine.open_orders.get(order_id)
    if order is None:
        order = next((o for o in engine.closed_orders if o.paper_order_id == order_id), None)
    if order is None:
        return error_response("NOT_FOUND", f"Paper order {order_id} not found")
    return success_response({
        "order": order.model_dump(mode="json"),
        "cost_reporting": engine.cost_reporting(),
        "cost_report": engine.order_cost_report(order),
    })


@router.get("/performance")
async def performance(context: AppContext = Depends(get_context)) -> dict:
    return success_response(context.paper_engine.performance().model_dump(mode="json"))
