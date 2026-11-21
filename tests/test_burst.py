"""End-to-end tests for burst-demand registration and fair quotas."""

from __future__ import annotations

from fractions import Fraction

from tests.conftest import links, tunnel


def create(client, scenario):
    return client.post("/rehearsals", json=scenario)


def reserve(client, rid, **body):
    return client.post(f"/rehearsals/{rid}/reservations", json=body or None)


def confirm(client, rid, resid):
    return client.post(f"/rehearsals/{rid}/reservations/{resid}/confirm")


def cancel(client, rid, resid):
    return client.post(f"/rehearsals/{rid}/reservations/{resid}/cancel")


def put_demands(client, rid, payload, method="put"):
    return client.request(method, f"/rehearsals/{rid}/burst-demands", json=payload)


def quota_map(view):
    return {item["tunnel_id"]: item["quota"] for item in view["burst"]["quotas"]}


def as_fraction(rendered):
    value = rendered["value"]
    if isinstance(value, int):
        return Fraction(value)
    numerator, denominator = value.split("/")
    return Fraction(int(numerator), int(denominator))


def simple_scenario():
    """Two parallel links between A and B, two tunnels swapping over them."""
    return {
        "links": links(("L1", "A", "B", 10), ("L3", "A", "B", 10)),
        "tunnels": [
            tunnel("T1", 4, ["L1"], ["L3"]),
            tunnel("T2", 3, ["L3"], ["L1"]),
        ],
    }


# ------------------------------------------------------------- registration
def test_register_bare_map_returns_whole_group_quotas(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    response = put_demands(client, rid, {"T1": 10, "T2": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rehearsal_id"] == rid
    assert body["burst"]["demands"] == {"T1": 10, "T2": 10}
    quotas = {item["tunnel_id"]: item["quota"] for item in body["burst"]["quotas"]}
    # Disjoint links: each takes what its own link can spare, capped by demand.
    assert as_fraction(quotas["T1"]) == 6
    assert as_fraction(quotas["T2"]) == 7
    # Exact fraction rendering for whole numbers stays an integer.
    assert quotas["T1"]["value"] == 6
    assert quotas["T1"]["numerator"] == 6 and quotas["T1"]["denominator"] == 1


def test_omitted_tunnels_default_to_zero_and_overwrite_previous_demand(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 5, "T2": 5})
    # Whole-group replacement: T2 missing now means T2 resets to 0.
    response = put_demands(client, rid, {"T1": 5})
    demands = response.json()["burst"]["demands"]
    assert demands == {"T1": 5, "T2": 0}
    quotas = quota_map(response.json())
    assert as_fraction(quotas["T1"]) == 5
    assert as_fraction(quotas["T2"]) == 0


def test_alternative_input_shapes_are_accepted(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]

    wrapped = put_demands(client, rid, {"demands": {"T1": 2, "T2": 3}})
    assert wrapped.status_code == 200
    assert wrapped.json()["burst"]["demands"] == {"T1": 2, "T2": 3}

    listed = put_demands(
        client,
        rid,
        {"demands": [
            {"tunnel_id": "T1", "demand": 4},
            {"tunnel_id": "T2", "demand": 1},
        ]},
    )
    assert listed.status_code == 200
    assert listed.json()["burst"]["demands"] == {"T1": 4, "T2": 1}

    bare_list = put_demands(
        client,
        rid,
        [{"tunnel_id": "T1", "demand": 2}, {"tunnel_id": "T2", "demand": 2}],
    )
    assert bare_list.status_code == 200
    assert bare_list.json()["burst"]["demands"] == {"T1": 2, "T2": 2}

    # POST alias behaves like PUT.
    post = put_demands(client, rid, {"T1": 1}, method="post")
    assert post.status_code == 200
    assert post.json()["burst"]["demands"] == {"T1": 1, "T2": 0}


def test_empty_body_zeros_the_whole_group(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 5, "T2": 5})
    response = client.put(f"/rehearsals/{rid}/burst-demands")
    assert response.status_code == 200
    assert response.json()["burst"]["demands"] == {"T1": 0, "T2": 0}


def test_fractional_fair_share_is_rendered_exactly(client):
    # Parallel links: after the first move both tunnels share one link with
    # 3 Mbps of spare, so the fair share is the exact fraction 3/2.
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    first = reserve(client, rid).json()
    confirm(client, rid, first["reservation_id"])
    response = put_demands(client, rid, {"T1": 10, "T2": 10})
    quotas = quota_map(response.json())
    assert as_fraction(quotas["T1"]) == Fraction(3, 2)
    assert as_fraction(quotas["T2"]) == Fraction(3, 2)
    assert quotas["T1"]["value"] == "3/2"
    assert quotas["T1"]["numerator"] == 3
    assert quotas["T1"]["denominator"] == 2


# ---------------------------------------------------------------- validation
def test_unknown_tunnel_is_rejected_and_state_is_kept(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 5, "T2": 5})
    response = put_demands(client, rid, {"T1": 9, "Nope": 1})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_burst_demands"
    view = client.get(f"/rehearsals/{rid}").json()
    # The failed registration kept the previous demands.
    assert view["burst"]["demands"] == {"T1": 5, "T2": 5}


def test_negative_and_non_integer_demands_are_rejected(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    assert put_demands(client, rid, {"T1": -1}).status_code == 422
    assert put_demands(client, rid, {"T1": 1.5}).status_code == 422
    assert put_demands(client, rid, {"T1": True}).status_code == 422
    # Nothing was written.
    view = client.get(f"/rehearsals/{rid}").json()
    assert view["burst"]["demands"] == {"T1": 0, "T2": 0}


def test_unknown_rehearsal_is_404(client):
    response = put_demands(client, "missing", {"T1": 1})
    assert response.status_code == 404
    assert response.json()["error"] == "rehearsal_not_found"


# ------------------------------------------------- integration with rerouting
def test_pending_reservation_takes_precedence_over_burst(client):
    # Links without endpoint metadata: T1 reuses L1 and adds L4; T2 holds L4.
    # Reserving T1 charges its bandwidth on L4 and saturates it for burst use.
    scenario = {
        "links": [
            {"link_id": "L1", "capacity": 10},
            {"link_id": "L2", "capacity": 10},
            {"link_id": "L4", "capacity": 10},
        ],
        "tunnels": [
            tunnel("T1", 4, ["L1", "L2"], ["L1", "L4"]),
            tunnel("T2", 6, ["L4"], ["L2"]),
        ],
    }
    rid = create(client, scenario).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 10, "T2": 10})
    before = client.get(f"/rehearsals/{rid}").json()
    l4_before = next(link for link in before["links"] if link["link_id"] == "L4")
    free_before = l4_before["available"]  # L4: 10 - 6 = 4

    pending = reserve(client, rid)
    assert pending.status_code == 201
    view = client.get(f"/rehearsals/{rid}").json()
    l4 = next(link for link in view["links"] if link["link_id"] == "L4")
    # The reservation holds 4 on L4, leaving no spare there at all.
    assert l4["reserved"] == 4
    assert l4["available"] == free_before - 4 == 0
    t2 = next(item for item in view["burst"]["quotas"] if item["tunnel_id"] == "T2")
    # T2's active path is L4; with zero spare, its burst quota collapses to 0.
    assert as_fraction(t2["quota"]) == 0
    # And the quota reported inside the reservation response matches the view.
    res_quotas = quota_map(pending.json())
    assert res_quotas == quota_map(view)


def test_cancelling_reservation_returns_the_spare(client):
    scenario = {
        "links": [
            {"link_id": "L1", "capacity": 10},
            {"link_id": "L2", "capacity": 10},
            {"link_id": "L4", "capacity": 10},
        ],
        "tunnels": [
            tunnel("T1", 4, ["L1", "L2"], ["L1", "L4"]),
            tunnel("T2", 6, ["L4"], ["L2"]),
        ],
    }
    rid = create(client, scenario).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 10, "T2": 10})
    baseline = client.get(f"/rehearsals/{rid}").json()

    reservation = reserve(client, rid).json()
    cancel(client, rid, reservation["reservation_id"])
    restored = client.get(f"/rehearsals/{rid}").json()
    # Cancelling gives the reserved bandwidth back; the quotas return exactly.
    assert restored["burst"] == baseline["burst"]
    assert all(link["reserved"] == 0 for link in restored["links"])


def test_confirm_reallocates_against_the_new_active_path(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 10, "T2": 10})
    before = quota_map(client.get(f"/rehearsals/{rid}").json())
    # Disjoint parallel links: each tunnel takes its own link's spare.
    assert as_fraction(before["T1"]) == 6 and as_fraction(before["T2"]) == 7

    reservation = reserve(client, rid).json()
    confirmed = confirm(client, rid, reservation["reservation_id"]).json()
    after = quota_map(confirmed)
    # After the first switch both tunnels share one link with 3 Mbps spare.
    assert as_fraction(after["T1"]) == Fraction(3, 2)
    assert as_fraction(after["T2"]) == Fraction(3, 2)
    # The response of the confirm and a fresh view describe the same state.
    view = client.get(f"/rehearsals/{rid}").json()
    assert quota_map(view) == after


def test_view_keeps_demand_quota_path_and_usage_consistent(client):
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    put_demands(client, rid, {"T1": 10, "T2": 10})
    view = client.get(f"/rehearsals/{rid}").json()

    quotas = quota_map(view)
    by_tunnel = {t["tunnel_id"]: t for t in view["tunnels"]}
    by_link = {link["link_id"]: link for link in view["links"]}

    # Per-tunnel quota equals the whole-group quota, and the stored demand too.
    for tunnel_id, quota in quotas.items():
        assert as_fraction(by_tunnel[tunnel_id]["burst_quota"]) == as_fraction(quota)
        assert by_tunnel[tunnel_id]["burst_demand"] == view["burst"]["demands"][tunnel_id]

    # Per-link burst usage equals the sum of quotas of tunnels traversing it,
    # and base + burst never exceeds capacity.
    for link_id, link in by_link.items():
        members = [
            tid
            for tid, t in by_tunnel.items()
            if link_id in t["current_path"]
        ]
        expected = sum((as_fraction(quotas[tid]) for tid in members), Fraction(0))
        assert as_fraction(link["burst_used"]) == expected
        assert as_fraction(link["burst_available"]) == Fraction(
            link["capacity"] - link["used"] - link["reserved"]
        ) - expected
        assert link["used"] + link["reserved"] <= link["capacity"]


def test_old_rehearsal_without_burst_data_reads_zero_demands(client):
    # Simulate a document written before burst support existed.
    engine = client.app.state.engine
    rid = create(client, simple_scenario()).json()["rehearsal_id"]
    document, version = engine._store.get(rid)
    document.pop("burst_demands")
    engine._store.replace(rid, version, document)

    view = client.get(f"/rehearsals/{rid}").json()
    assert view["burst"]["demands"] == {"T1": 0, "T2": 0}
    assert all(
        as_fraction(item["quota"]) == 0 for item in view["burst"]["quotas"]
    )
    # Registering still works and upgrades the document.
    response = put_demands(client, rid, {"T1": 2})
    assert response.status_code == 200
    assert response.json()["burst"]["demands"] == {"T1": 2, "T2": 0}
