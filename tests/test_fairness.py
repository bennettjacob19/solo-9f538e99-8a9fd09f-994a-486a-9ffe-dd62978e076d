"""Unit tests for the exact max-min fair burst allocator."""

from __future__ import annotations

from fractions import Fraction

import pytest

from app.domain import LinkRecord, RehearsalRecord, ReservationRecord, TunnelRecord
from app.engine import Engine
from app.fairness import fraction_payload, max_min_fair


def _record(link_specs, tunnel_specs, moved=(), demands=None, charge=None):
    links = tuple(
        LinkRecord(link_id, None, None, capacity)
        for link_id, capacity in link_specs
    )
    tunnels = tuple(
        TunnelRecord(
            tunnel_id,
            bandwidth,
            tuple(current),
            tuple(target),
            moved=tunnel_id in moved,
        )
        for tunnel_id, bandwidth, current, target in tunnel_specs
    )
    record = RehearsalRecord(
        "r1",
        "now",
        links,
        tunnels,
        tuple(tunnel.tunnel_id for tunnel in tunnels),
        burst_demands=dict(demands or {}),
    )
    if charge is not None:
        record.reservations["res_1"] = ReservationRecord(
            "res_1", tunnels[0].tunnel_id, 1, "pending", charge=dict(charge)
        )
        record.pending = "res_1"
    return record


def _alloc(record):
    return max_min_fair(record, Engine._usage(record), Engine._reserved(record))


def test_equal_share_is_exact_fraction():
    # One link, cap 10, 4 already used -> spare 6 split equally.
    record = _record(
        [("L1", 10)],
        [
            ("T1", 4, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands={"T1": 10, "T2": 10},
    )
    quotas, burst_used, spare = _alloc(record)
    assert quotas == {"T1": Fraction(3), "T2": Fraction(3)}
    assert burst_used["L1"] == 6
    assert spare["L1"] == 0


def test_quotas_are_never_more_than_the_demand():
    record = _record(
        [("L1", 10)],
        [
            ("T1", 4, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands={"T1": 1, "T2": 10},
    )
    quotas, *_ = _alloc(record)
    # T1 stops at its demand; T2 then takes the rest of the spare.
    assert quotas["T1"] == 1
    assert quotas["T2"] == 5


def test_water_filling_past_a_saturated_link():
    # L1 carries both tunnels (cap 18, base 8 from T2 -> spare 10),
    # L2 carries only T2 (cap 10, base 8 -> spare 2).  T2 freezes at 2,
    # T1 keeps climbing to 8.
    record = _record(
        [("L1", 18), ("L2", 10)],
        [
            ("T1", 0, ["L1"], ["L1"]),
            ("T2", 8, ["L1", "L2"], ["L1", "L2"]),
        ],
        demands={"T1": 10, "T2": 10},
    )
    quotas, burst_used, spare = _alloc(record)
    assert quotas["T2"] == 2
    assert quotas["T1"] == 8
    assert burst_used["L1"] == 10
    assert burst_used["L2"] == 2
    assert spare["L1"] == 0 and spare["L2"] == 0


def test_three_way_split_keeps_thirds_exactly():
    record = _record(
        [("L1", 10)],
        [(f"T{i}", 0, ["L1"], ["L1"]) for i in range(3)],
        demands={f"T{i}": 10 for i in range(3)},
    )
    quotas, burst_used, _ = _alloc(record)
    assert all(value == Fraction(10, 3) for value in quotas.values())
    assert burst_used["L1"] == 10


def test_zero_spare_freezes_everyone_at_zero():
    record = _record(
        [("L1", 4)],
        [
            ("T1", 4, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands={"T1": 10, "T2": 10},
    )
    quotas, burst_used, spare = _alloc(record)
    assert all(value == 0 for value in quotas.values())
    assert burst_used["L1"] == 0
    assert spare["L1"] == 0


def test_pending_reservation_is_deducted_first():
    # Base 4/10 used, a pending reservation holds another 4 -> spare 2,
    # so the two burst flows share only 1 Mbps each.
    record = _record(
        [("L1", 10)],
        [
            ("T1", 4, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands={"T1": 10, "T2": 10},
        charge={"L1": 4},
    )
    quotas, burst_used, spare = _alloc(record)
    assert quotas["T1"] == quotas["T2"] == 1
    assert burst_used["L1"] == 2
    assert spare["L1"] == 0


def test_quota_follows_the_active_path_after_a_reroute():
    # Before any move the tunnels sit on disjoint links.
    record = _record(
        [("L1", 10), ("L3", 10)],
        [
            ("T1", 4, ["L1"], ["L3"]),
            ("T2", 3, ["L3"], ["L1"]),
        ],
        demands={"T1": 10, "T2": 10},
    )
    before, _, _ = _alloc(record)
    assert before["T1"] == 6 and before["T2"] == 7

    # T1 switches onto L3: both tunnels now share L3 (7 used, 3 free).
    moved = _record(
        [("L1", 10), ("L3", 10)],
        [
            ("T1", 4, ["L1"], ["L3"]),
            ("T2", 3, ["L3"], ["L1"]),
        ],
        moved={"T1"},
        demands={"T1": 10, "T2": 10},
    )
    after, burst_used, spare = _alloc(moved)
    assert after["T1"] == after["T2"] == Fraction(3, 2)
    assert burst_used["L3"] == 3
    assert spare["L3"] == 0
    # L1 carries no active tunnel any more, so it carries no burst either.
    assert burst_used["L1"] == 0
    assert spare["L1"] == 10


def test_unregistered_and_zero_demand_tunnels_get_zero():
    record = _record(
        [("L1", 10)],
        [
            ("T1", 0, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands={"T1": 5},
    )
    quotas, *_ = _alloc(record)
    assert quotas["T1"] == 5
    assert quotas["T2"] == 0

    bare = _record(
        [("L1", 10)],
        [
            ("T1", 0, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
    )
    quotas, *_ = _alloc(bare)
    assert all(value == 0 for value in quotas.values())


def test_fraction_payload_rendering():
    assert fraction_payload(Fraction(3, 2)) == {
        "value": "3/2",
        "numerator": 3,
        "denominator": 2,
    }
    assert fraction_payload(Fraction(4)) == {
        "value": 4,
        "numerator": 4,
        "denominator": 1,
    }


@pytest.mark.parametrize(
    "demands,expected",
    [
        ({"T1": 0, "T2": 0}, {"T1": 0, "T2": 0}),
        ({"T1": 7}, {"T1": 7, "T2": 0}),
    ],
)
def test_demand_lookup_defaults_to_zero(demands, expected):
    record = _record(
        [("L1", 10)],
        [
            ("T1", 0, ["L1"], ["L1"]),
            ("T2", 0, ["L1"], ["L1"]),
        ],
        demands=demands,
    )
    quotas, *_ = _alloc(record)
    assert quotas == expected
