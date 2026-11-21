"""HTTP layer: thin translation between the engine and JSON."""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from .domain import RehearsalRecord
from .fairness import Fraction, fraction_payload
from .models import (
    BurstAllocationOut,
    BurstDemandsIn,
    BurstDemandsOut,
    BurstLinkOut,
    BurstQuotaOut,
    FractionOut,
    LinkUsageOut,
    OrderStepOut,
    PendingOut,
    RehearsalCreatedOut,
    RehearsalIn,
    RehearsalViewOut,
    ReservationOut,
    ReserveIn,
    ResolutionOut,
    TunnelStatusOut,
)

router = APIRouter(prefix="/rehearsals", tags=["rehearsals"])


def _engine(request: Request):
    return request.app.state.engine


def _steps(record: RehearsalRecord) -> list[OrderStepOut]:
    return [
        OrderStepOut(
            sequence=i + 1,
            tunnel_id=tunnel_id,
            bandwidth=record.tunnel(tunnel_id).bandwidth,  # type: ignore[union-attr]
        )
        for i, tunnel_id in enumerate(record.order)
    ]


def _fraction(value: Fraction) -> FractionOut:
    return FractionOut(**fraction_payload(value))


def _burst_payload(record, quotas, burst_used, spare) -> BurstAllocationOut:
    """Assemble the burst response from an already-taken consistent snapshot."""
    demands = {
        tunnel.tunnel_id: record.burst_demand_of(tunnel.tunnel_id)
        for tunnel in record.tunnels
    }
    return BurstAllocationOut(
        demands=demands,
        quotas=[
            BurstQuotaOut(
                tunnel_id=tunnel.tunnel_id,
                demand=demands[tunnel.tunnel_id],
                quota=_fraction(quotas[tunnel.tunnel_id]),
                active_path=list(tunnel.active_path),
            )
            for tunnel in record.tunnels
        ],
        links=[
            BurstLinkOut(
                link_id=link.link_id,
                used=_fraction(burst_used[link.link_id]),
                available=_fraction(spare[link.link_id]),
            )
            for link in record.links
        ],
    )


def _burst_allocation(engine, rehearsal_id: str) -> BurstAllocationOut:
    """Take one snapshot and build the whole-group burst response."""
    record, _used, _reserved, quotas, burst_used, spare = engine.burst_snapshot(
        rehearsal_id
    )
    return _burst_payload(record, quotas, burst_used, spare)


@router.post(
    "",
    response_model=RehearsalCreatedOut,
    status_code=201,
    summary="Submit a scenario and start a rehearsal",
    responses={
        409: {"description": "No feasible order; no rehearsal was created."},
        422: {"description": "The scenario is structurally invalid."},
    },
)
def create(rehearsal: RehearsalIn, request: Request):
    record = _engine(request).create_rehearsal(rehearsal.links, rehearsal.tunnels)
    return RehearsalCreatedOut(
        rehearsal_id=record.rehearsal_id,
        status="completed" if record.completed else "active",
        created_at=record.created_at,
        order=list(record.order),
        steps=_steps(record),
        tunnels_total=len(record.tunnels),
        links_total=len(record.links),
    )


@router.get(
    "/{rehearsal_id}",
    response_model=RehearsalViewOut,
    summary="Inspect active paths, per-link usage and the burst allocation",
)
def view(rehearsal_id: str, request: Request):
    engine = _engine(request)
    record, used, reserved, quotas, burst_used, spare = engine.burst_snapshot(
        rehearsal_id
    )
    pending = None
    if record.pending is not None:
        reservation = record.reservations[record.pending]
        pending = PendingOut(
            reservation_id=reservation.reservation_id,
            tunnel_id=reservation.tunnel_id,
            sequence=reservation.sequence,
            reserved=reservation.charge,
        )
    return RehearsalViewOut(
        rehearsal_id=record.rehearsal_id,
        status="completed" if record.completed else "active",
        created_at=record.created_at,
        order=list(record.order),
        remaining_order=list(record.remaining_order),
        next_tunnel_id=record.next_tunnel_id,
        tunnels_total=len(record.tunnels),
        tunnels_moved=record.moved_count,
        tunnels=[
            TunnelStatusOut(
                tunnel_id=t.tunnel_id,
                bandwidth=t.bandwidth,
                moved=t.moved,
                current_path=list(t.active_path),
                initial_path=list(t.current_path),
                target_path=list(t.target_path),
                burst_demand=record.burst_demand_of(t.tunnel_id),
                burst_quota=_fraction(quotas[t.tunnel_id]),
            )
            for t in record.tunnels
        ],
        links=[
            LinkUsageOut(
                link_id=link.link_id,
                source=link.source,
                target=link.target,
                capacity=link.capacity,
                used=used[link.link_id],
                reserved=reserved[link.link_id],
                available=link.capacity - used[link.link_id] - reserved[link.link_id],
                burst_used=_fraction(burst_used[link.link_id]),
                burst_available=_fraction(spare[link.link_id]),
            )
            for link in record.links
        ],
        pending=pending,
        burst=_burst_payload(record, quotas, burst_used, spare),
    )


@router.post(
    "/{rehearsal_id}/reservations",
    response_model=ReservationOut,
    status_code=201,
    summary="Reserve bandwidth for the next tunnel (one pending at a time)",
    responses={409: {"description": "Out of order, or another reservation is pending."}},
)
def reserve(
    rehearsal_id: str,
    request: Request,
    body: ReserveIn | None = Body(default=None),
):
    tunnel_id = body.tunnel_id if body is not None else None
    engine = _engine(request)
    reservation = engine.reserve(rehearsal_id, tunnel_id)
    record = engine.view(rehearsal_id)[0]
    tunnel = record.tunnel(reservation.tunnel_id)
    assert tunnel is not None
    return ReservationOut(
        rehearsal_id=record.rehearsal_id,
        reservation_id=reservation.reservation_id,
        tunnel_id=reservation.tunnel_id,
        sequence=reservation.sequence,
        status=reservation.status,
        reserved=reservation.charge,
        current_path=list(tunnel.active_path),
        target_path=list(tunnel.target_path),
        next_tunnel_id=record.next_tunnel_id,
        burst=_burst_allocation(engine, rehearsal_id),
    )


@router.post(
    "/{rehearsal_id}/reservations/{reservation_id}/confirm",
    response_model=ResolutionOut,
    summary="Confirm a pending reservation: switch the path and free the old one",
    responses={409: {"description": "Unknown, stale or non-pending reservation id."}},
)
def confirm(rehearsal_id: str, reservation_id: str, request: Request):
    engine = _engine(request)
    reservation, tunnel = engine.confirm(rehearsal_id, reservation_id)
    record = engine.view(rehearsal_id)[0]
    return ResolutionOut(
        rehearsal_id=record.rehearsal_id,
        reservation_id=reservation.reservation_id,
        tunnel_id=reservation.tunnel_id,
        sequence=reservation.sequence,
        status=reservation.status,
        current_path=list(tunnel.active_path),
        released=tunnel.released(),
        rehearsal_status="completed" if record.completed else "active",
        next_tunnel_id=record.next_tunnel_id,
        burst=_burst_allocation(engine, rehearsal_id),
    )


@router.post(
    "/{rehearsal_id}/reservations/{reservation_id}/cancel",
    response_model=ResolutionOut,
    summary="Cancel a pending reservation; the step can be retried",
    responses={409: {"description": "Unknown, stale or non-pending reservation id."}},
)
def cancel(rehearsal_id: str, reservation_id: str, request: Request):
    engine = _engine(request)
    reservation = engine.cancel(rehearsal_id, reservation_id)
    record = engine.view(rehearsal_id)[0]
    tunnel = record.tunnel(reservation.tunnel_id)
    assert tunnel is not None
    return ResolutionOut(
        rehearsal_id=record.rehearsal_id,
        reservation_id=reservation.reservation_id,
        tunnel_id=reservation.tunnel_id,
        sequence=reservation.sequence,
        status=reservation.status,
        current_path=list(tunnel.active_path),
        released={},
        rehearsal_status="completed" if record.completed else "active",
        next_tunnel_id=record.next_tunnel_id,
        burst=_burst_allocation(engine, rehearsal_id),
    )


@router.put(
    "/{rehearsal_id}/burst-demands",
    response_model=BurstDemandsOut,
    summary="Replace the whole group of burst demands and return the new quotas",
    responses={
        404: {"description": "Unknown rehearsal id."},
        422: {"description": "Unknown tunnel, negative or non-integer demand; state unchanged."},
    },
)
@router.post(
    "/{rehearsal_id}/burst-demands",
    response_model=BurstDemandsOut,
    include_in_schema=False,
)
def replace_burst_demands(
    rehearsal_id: str,
    request: Request,
    body: BurstDemandsIn | None = Body(default=None),
):
    """Whole-group replacement; omitted tunnels are written back as 0.

    On any validation failure nothing is stored, so the previously registered
    demands (and the quotas derived from them) stay in force.
    """
    engine = _engine(request)
    demands = body.normalised() if body is not None else {}
    engine.register_burst(rehearsal_id, demands)
    return BurstDemandsOut(
        rehearsal_id=rehearsal_id,
        burst=_burst_allocation(engine, rehearsal_id),
    )
