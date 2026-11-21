"""Exact max-min fair allocation of spare link bandwidth.

Each tunnel asks for an extra (burst) bandwidth ``d_t`` (a non-negative integer
Mbps).  A tunnel carries *one* quota ``q_t`` on **every** link of its currently
active path -- the same value along the whole path -- and never more than it
asked for: ``0 <= q_t <= d_t``.

On every link ``l`` the burst quotas share only the bandwidth not already taken
by the tunnels' base occupancy and by a pending migration reservation:

    sum(t in tunnels(l), q_t) <= spare_l = capacity_l - used_l - reserved_l.

The allocation is the lexicographically maximal (max-min fair) vector under
those constraints, computed by the classic progressive-filling (water-filling)
algorithm.  Every quantity is a :class:`fractions.Fraction`, so the result is an
exact rational number even when a fair share is fractional (e.g. ``5/2``); no
floating-point arithmetic is used anywhere.
"""

from __future__ import annotations

from fractions import Fraction

from .domain import RehearsalRecord


def max_min_fair(
    record: RehearsalRecord,
    used: dict[str, int],
    reserved: dict[str, int],
) -> tuple[dict[str, Fraction], dict[str, Fraction], dict[str, Fraction]]:
    """Return ``(quotas, burst_used, spare)`` for the current state.

    * ``quotas[tunnel_id]`` -- the exact burst quota (Fraction) each tunnel may
      send on every link of its active path, capped at its registered demand.
    * ``burst_used[link_id]`` -- sum of the quotas of the tunnels whose active
      path traverses the link.
    * ``spare[link_id]`` -- capacity left after base usage, the pending
      reservation and the allocated burst traffic.

    Tunnels with a demand of 0 (the default for unregistered/old rehearsals)
    take no part in the allocation and always receive a quota of 0.
    """
    capacities = {link.link_id: link.capacity for link in record.links}
    link_ids = list(capacities)

    # Spare bandwidth per link after base occupancy and any held reservation.
    spare: dict[str, Fraction] = {
        link_id: Fraction(capacities[link_id] - used[link_id] - reserved[link_id])
        for link_id in link_ids
    }

    demands: dict[str, int] = {
        tunnel.tunnel_id: record.burst_demand_of(tunnel.tunnel_id)
        for tunnel in record.tunnels
    }
    # Tunnels whose active path references a declared link; a demand of 0 means
    # the tunnel does not participate and keeps a quota of 0.
    paths: dict[str, frozenset[str]] = {
        tunnel.tunnel_id: frozenset(
            link_id for link_id in tunnel.active_path if link_id in capacities
        )
        for tunnel in record.tunnels
    }
    unfrozen = {
        tid
        for tid, demand in demands.items()
        if demand > 0 and paths[tid]
    }

    quotas: dict[str, Fraction] = {
        tunnel.tunnel_id: Fraction(0) for tunnel in record.tunnels
    }

    # Progressive filling: raise every not-yet-frozen quota by the same amount
    # until some tunnel reaches its own demand, or some link is saturated.  The
    # frozen tunnels keep their level; the rest keep climbing together.
    while unfrozen:
        members: dict[str, set[str]] = {
            link_id: {tid for tid in unfrozen if link_id in paths[tid]}
            for link_id in link_ids
        }

        # Smallest level increase before (a) a demand cap or (b) a link fills.
        delta: Fraction | None = None
        for tid in unfrozen:
            candidate = Fraction(demands[tid]) - quotas[tid]
            delta = candidate if delta is None or candidate < delta else delta
        for link_id in link_ids:
            count = len(members[link_id])
            if count:
                candidate = spare[link_id] / count
                delta = candidate if delta is None or candidate < delta else delta
        assert delta is not None and delta >= 0

        if delta > 0:
            for tid in unfrozen:
                quotas[tid] += delta
            for link_id in link_ids:
                count = len(members[link_id])
                if count:
                    spare[link_id] -= delta * count

        # Freeze every tunnel blocked at this level: it is either fully served
        # (quota == demand) or it traverses a link that is now saturated.
        blocked: set[str] = set()
        for tid in unfrozen:
            if quotas[tid] >= demands[tid]:
                blocked.add(tid)
                continue
            for link_id in paths[tid]:
                if spare[link_id] == 0:
                    blocked.add(tid)
                    break
        # Exact arithmetic guarantees progress; guard anyway against a zero
        # delta with nothing recognised as blocked.
        if not blocked:  # pragma: no cover - defensive
            blocked = set(unfrozen)
        unfrozen -= blocked

    burst_used: dict[str, Fraction] = {link_id: Fraction(0) for link_id in link_ids}
    for tid, quota in quotas.items():
        if quota:
            for link_id in paths[tid]:
                burst_used[link_id] += quota

    return quotas, burst_used, spare


def fraction_payload(value: Fraction) -> dict[str, object]:
    """JSON-friendly exact rendering of a fraction.

    Whole numbers render as a plain integer ``value``; otherwise ``value`` is
    the canonical ``"n/d"`` string.  ``numerator``/``denominator`` are always
    present as integers for clients that prefer structured fields.
    """
    numerator, denominator = value.numerator, value.denominator
    if denominator == 1:
        rendered: object = numerator
    else:
        rendered = f"{numerator}/{denominator}"
    return {
        "value": rendered,
        "numerator": numerator,
        "denominator": denominator,
    }
