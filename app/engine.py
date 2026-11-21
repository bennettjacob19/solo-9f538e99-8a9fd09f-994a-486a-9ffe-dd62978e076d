"""Rehearsal engine.

All state transitions live here.  They run under a process-wide lock *and* are
committed with compare-and-set against the stored version, so concurrent
requests (threads, or several uvicorn workers sharing the SQLite file) can never
interleave two half-applied transitions.  A lost race is retried: every
transition is a pure function of the freshly loaded document, and re-validation
is part of it, so a retry re-evaluates the world instead of replaying a stale
decision.

Invariants
----------
* At most one *pending* reservation per rehearsal.
* Reservation requests are accepted only for the next tunnel of the stored
  order; anything else is rejected and changes nothing.
* A pending reservation holds bandwidth only on the target-path links that are
  *not* on the current path.  Links shared by both paths are already occupied
  by the tunnel's old path, so they are counted once, not twice.
* The old path keeps occupying bandwidth until its reservation is confirmed.
* Confirming switches the tunnel and implicitly frees the bandwidth that only
  the old path used.
* Cancelling drops the pending reservation and nothing else, so the step can be
  retried.
* Unknown or stale reservation ids never change state.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from .domain import RehearsalRecord, ReservationRecord, TunnelRecord
from .errors import (
    BurstDemandError,
    Infeasible,
    RehearsalNotFound,
    ReservationInvalid,
    StepConflict,
)
from .fairness import Fraction, max_min_fair
from .planner import explain_infeasible, find_order
from .store import Store, StoreConflict
from .validation import build_scenario

MAX_WRITE_RETRIES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Engine:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self._store.initialize()

    def close(self) -> None:
        self._store.close()

    # ------------------------------------------------------------- load/store
    def _load(self, rehearsal_id: str) -> tuple[RehearsalRecord, int]:
        entry = self._store.get(rehearsal_id)
        if entry is None:
            raise RehearsalNotFound(f"rehearsal {rehearsal_id!r} does not exist")
        document, version = entry
        return RehearsalRecord.from_document(document), version

    def _read(self, rehearsal_id: str) -> RehearsalRecord:
        return self._load(rehearsal_id)[0]

    # ---------------------------------------------------------------- create
    def create_rehearsal(self, link_specs, tunnel_specs) -> RehearsalRecord:
        scenario = build_scenario(link_specs, tunnel_specs)
        order = find_order(scenario.links, scenario.tunnels)
        if order is None:
            raise Infeasible(explain_infeasible(scenario.links, scenario.tunnels))

        record = RehearsalRecord(
            rehearsal_id=_new_id("reh"),
            created_at=_now(),
            links=scenario.links,
            tunnels=scenario.tunnels,
            order=tuple(order),
        )
        with self._lock:
            self._store.create(record.to_document())
        return record

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _usage(record: RehearsalRecord) -> dict[str, int]:
        used = {link.link_id: 0 for link in record.links}
        for tunnel in record.tunnels:
            for link_id in tunnel.active_path:
                used[link_id] += tunnel.bandwidth
        return used

    @staticmethod
    def _pending_reservation(record: RehearsalRecord) -> ReservationRecord | None:
        if record.pending is None:
            return None
        return record.reservations.get(record.pending)

    @classmethod
    def _reserved(cls, record: RehearsalRecord) -> dict[str, int]:
        """Bandwidth held by the pending reservation, per link (0 when none)."""
        reserved = {link.link_id: 0 for link in record.links}
        pending = cls._pending_reservation(record)
        if pending is not None:
            for link_id, bandwidth in pending.charge.items():
                reserved[link_id] += bandwidth
        return reserved

    def _check_reservable(self, record: RehearsalRecord, tunnel: TunnelRecord) -> None:
        used = self._usage(record)
        capacities = {link.link_id: link.capacity for link in record.links}
        for link_id, bandwidth in tunnel.charge().items():
            if used[link_id] + bandwidth > capacities[link_id]:
                raise StepConflict(
                    f"link {link_id!r} has {capacities[link_id] - used[link_id]} Mbps "
                    f"free but reserving tunnel {tunnel.tunnel_id!r} needs "
                    f"{bandwidth} Mbps"
                )

    # --------------------------------------------------------------- reserve
    def reserve(self, rehearsal_id: str, tunnel_id: str | None = None) -> ReservationRecord:
        for _ in range(MAX_WRITE_RETRIES):
            with self._lock:
                record, version = self._load(rehearsal_id)
                self._validate_reserve(record, tunnel_id)
                assert record.next_tunnel_id is not None
                tunnel = record.tunnel(record.next_tunnel_id)
                assert tunnel is not None
                self._check_reservable(record, tunnel)

                reservation = ReservationRecord(
                    reservation_id=_new_id("res"),
                    tunnel_id=tunnel.tunnel_id,
                    sequence=record.sequence_of(tunnel.tunnel_id),
                    status="pending",
                    charge=tunnel.charge(),
                    created_at=_now(),
                )
                record.reservations[reservation.reservation_id] = reservation
                record.pending = reservation.reservation_id
                try:
                    self._store.replace(rehearsal_id, version, record.to_document())
                except StoreConflict:
                    continue
                return reservation
        raise StepConflict("the rehearsal is being modified concurrently; retry the request")

    @staticmethod
    def _validate_reserve(record: RehearsalRecord, tunnel_id: str | None) -> None:
        if record.pending is not None:
            raise StepConflict(
                f"reservation {record.pending!r} is still pending; "
                "confirm or cancel it first"
            )
        if record.completed:
            raise StepConflict("every tunnel has already been migrated; nothing to reserve")
        expected = record.next_tunnel_id
        if expected is None:  # pragma: no cover - completed covers this
            raise StepConflict("nothing left to reserve")
        if tunnel_id is not None and tunnel_id != expected:
            raise StepConflict(
                f"tunnels must be migrated in the planned order; next is {expected!r}, "
                f"not {tunnel_id!r}"
            )

    # --------------------------------------------------------------- resolve
    def confirm(
        self, rehearsal_id: str, reservation_id: str
    ) -> tuple[ReservationRecord, TunnelRecord]:
        for _ in range(MAX_WRITE_RETRIES):
            with self._lock:
                record, version = self._load(rehearsal_id)
                reservation = self._require_pending(record, reservation_id)

                tunnel = record.tunnel(reservation.tunnel_id)
                if tunnel is None:  # pragma: no cover - validated at creation
                    raise ReservationInvalid("reservation references an unknown tunnel")
                self._check_reservable(record, tunnel)

                updated = TunnelRecord(
                    tunnel_id=tunnel.tunnel_id,
                    bandwidth=tunnel.bandwidth,
                    current_path=tunnel.current_path,
                    target_path=tunnel.target_path,
                    moved=True,
                )
                record.replace_tunnel(updated)
                reservation.status = "confirmed"
                reservation.resolved_at = _now()
                record.pending = None
                try:
                    self._store.replace(rehearsal_id, version, record.to_document())
                except StoreConflict:
                    continue
                return reservation, updated
        raise StepConflict("the rehearsal is being modified concurrently; retry the request")

    def cancel(self, rehearsal_id: str, reservation_id: str) -> ReservationRecord:
        for _ in range(MAX_WRITE_RETRIES):
            with self._lock:
                record, version = self._load(rehearsal_id)
                reservation = self._require_pending(record, reservation_id)

                reservation.status = "cancelled"
                reservation.resolved_at = _now()
                record.pending = None
                try:
                    self._store.replace(rehearsal_id, version, record.to_document())
                except StoreConflict:
                    continue
                return reservation
        raise StepConflict("the rehearsal is being modified concurrently; retry the request")

    @staticmethod
    def _require_pending(
        record: RehearsalRecord, reservation_id: str
    ) -> ReservationRecord:
        reservation = record.reservations.get(reservation_id)
        if reservation is None:
            raise ReservationInvalid(
                f"reservation {reservation_id!r} is unknown or no longer valid"
            )
        if record.pending != reservation_id or reservation.status != "pending":
            raise ReservationInvalid(
                f"reservation {reservation_id!r} is not pending "
                f"(status: {reservation.status})"
            )
        return reservation

    # ------------------------------------------------------------- burst API
    def register_burst(
        self, rehearsal_id: str, demands: dict[str, int]
    ) -> RehearsalRecord:
        """Whole-group replacement of the burst demands.

        Every tunnel gets an entry: tunnels omitted from ``demands`` reset to
        0.  Validation happens before any write, so a rejected request leaves
        the previously registered demands (and everything else) untouched.
        """
        known = {tunnel.tunnel_id for tunnel in self._read(rehearsal_id).tunnels}
        errors: list[str] = []
        unknown = sorted(set(demands) - known)
        if unknown:
            errors.append(
                "unknown tunnel id(s) in burst demands: " + ", ".join(unknown)
            )
        bad = [
            tunnel_id
            for tunnel_id, demand in demands.items()
            if not isinstance(demand, int) or isinstance(demand, bool) or demand < 0
        ]
        if bad:
            errors.append(
                "burst demand must be a non-negative integer Mbps for: "
                + ", ".join(sorted(bad))
            )
        if errors:
            raise BurstDemandError(errors)

        replacement = {tunnel_id: int(demands.get(tunnel_id, 0)) for tunnel_id in known}
        for _ in range(MAX_WRITE_RETRIES):
            with self._lock:
                record, version = self._load(rehearsal_id)
                record.burst_demands = replacement
                try:
                    self._store.replace(rehearsal_id, version, record.to_document())
                except StoreConflict:
                    continue
                return record
        raise StepConflict("the rehearsal is being modified concurrently; retry the request")

    def burst_snapshot(
        self, rehearsal_id: str
    ) -> tuple[
        RehearsalRecord,
        dict[str, int],
        dict[str, int],
        dict[str, Fraction],
        dict[str, Fraction],
        dict[str, Fraction],
    ]:
        """One consistent snapshot: record, base usage, reservation, burst math.

        All six views are derived from the same document under the engine lock,
        so a reported demand/quota/path/link-usage tuple can never mix two
        states.
        """
        with self._lock:
            record = self._read(rehearsal_id)
            used = self._usage(record)
            reserved = self._reserved(record)
            quotas, burst_used, spare = max_min_fair(record, used, reserved)
            return record, used, reserved, quotas, burst_used, spare

    # ------------------------------------------------------------------ view
    def view(
        self, rehearsal_id: str
    ) -> tuple[RehearsalRecord, dict[str, int], dict[str, int]]:
        with self._lock:
            record = self._read(rehearsal_id)
            return record, self._usage(record), self._reserved(record)
