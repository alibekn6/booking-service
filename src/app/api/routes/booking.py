import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import BookingServiceDep, CurrentUser, limit_hold_rate
from app.api.schemas import ErrorOut, HoldCreateIn, HoldOut, OrderOut
from app.domain.views import HoldView, OrderView

router = APIRouter(tags=["booking"])

_ERRORS: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorOut} for code in (401, 404, 409, 410, 422, 429, 503)
}


@router.post(
    "/events/{event_id}/holds",
    status_code=201,
    response_model=HoldOut,
    responses=_ERRORS,
    dependencies=[Depends(limit_hold_rate)],
    summary="Hold specific seats, or any N free seats of a category",
    description=(
        "All or nothing: if any requested seat is taken nothing is held. "
        "Seats are held for 10 minutes. A buyer owns at most 4 seats per event, "
        "purchased ones included. Send `Idempotency-Key` to make retries safe."
    ),
)
async def create_hold(
    event_id: uuid.UUID,
    payload: HoldCreateIn,
    user: CurrentUser,
    booking: BookingServiceDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> HoldView:
    hold, replayed = await booking.hold(
        buyer_id=user.user_id,
        event_id=event_id,
        request=payload.to_command(),
        idempotency_key=idempotency_key,
    )
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return hold


@router.get("/holds/{hold_id}", response_model=HoldOut, responses=_ERRORS)
async def get_hold(hold_id: uuid.UUID, user: CurrentUser, booking: BookingServiceDep) -> HoldView:
    return await booking.get_hold(buyer_id=user.user_id, hold_id=hold_id)


@router.post(
    "/holds/{hold_id}/confirm",
    response_model=OrderOut,
    responses=_ERRORS,
    summary="Confirm (simulated payment); the seats become sold",
    description="Idempotent: repeating it returns the same order with 200. "
    "After the hold deadline: 410 HOLD_EXPIRED.",
)
async def confirm_hold(
    hold_id: uuid.UUID, user: CurrentUser, booking: BookingServiceDep
) -> OrderView:
    return await booking.confirm(buyer_id=user.user_id, hold_id=hold_id)


@router.delete(
    "/holds/{hold_id}",
    status_code=204,
    responses=_ERRORS,
    summary="Cancel a hold (idempotent)",
)
async def release_hold(
    hold_id: uuid.UUID, user: CurrentUser, booking: BookingServiceDep
) -> Response:
    await booking.release(buyer_id=user.user_id, hold_id=hold_id)
    return Response(status_code=204)


@router.get("/orders/{order_id}", response_model=OrderOut, responses=_ERRORS)
async def get_order(
    order_id: uuid.UUID, user: CurrentUser, booking: BookingServiceDep
) -> OrderView:
    return await booking.get_order(buyer_id=user.user_id, order_id=order_id)


@router.get("/me/holds", response_model=list[HoldOut], summary="My holds")
async def my_holds(
    user: CurrentUser,
    booking: BookingServiceDep,
    event_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[HoldView]:
    return await booking.list_holds(buyer_id=user.user_id, event_id=event_id, limit=limit)


@router.get("/me/orders", response_model=list[OrderOut], summary="My purchases")
async def my_orders(
    user: CurrentUser,
    booking: BookingServiceDep,
    event_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[OrderView]:
    return await booking.list_orders(buyer_id=user.user_id, event_id=event_id, limit=limit)
