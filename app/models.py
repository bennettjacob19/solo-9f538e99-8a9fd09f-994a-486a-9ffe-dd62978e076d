"""Request/response schemas for the rehearsal API.

Field names accept a few common aliases so the Swagger page (``/docs``) is
forgiving: ``id``/``link_id``, ``from``/``source``, ``to``/``target``,
``path``/``current_path``, ``new_path``/``target_path``, ``edges``/``links``,
``circuits``/``tunnels``.  Unknown extra fields are ignored.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

EXAMPLE_SCENARIO: dict[str, Any] = {
    "links": [
        {"link_id": "L1", "source": "A", "target": "B", "capacity": 10},
        {"link_id": "L2", "source": "B", "target": "C", "capacity": 10},
        {"link_id": "L3", "source": "A", "target": "C", "capacity": 5},
        {"link_id": "L4", "source": "C", "target": "D", "capacity": 10},
    ],
    "tunnels": [
        {
            "tunnel_id": "T1",
            "bandwidth": 4,
            "current_path": ["L1", "L2"],
            "target_path": ["L3"],
        },
        {
            "tunnel_id": "T2",
            "bandwidth": 3,
            "current_path": ["L3"],
            "target_path": ["L1", "L2"],
        },
    ],
}


class LinkIn(BaseModel):
    """A directed link with an integer capacity in Mbps.

    ``source``/``target`` are optional: when every link supplies them the paths
    are checked for contiguity and endpoint equality; when they are omitted the
    service still works, it just cannot make those geometric checks.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    link_id: Annotated[
        str,
        Field(
            min_length=1,
            validation_alias=AliasChoices("link_id", "id", "name", "link"),
            description="Unique link identifier used by tunnel paths.",
        ),
    ]
    source: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            validation_alias=AliasChoices("source", "src", "from", "start", "from_node", "tail"),
            description="Directed start node (optional).",
        ),
    ]
    target: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            validation_alias=AliasChoices("target", "dst", "to", "end", "to_node", "head"),
            description="Directed end node (optional).",
        ),
    ]
    capacity: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            validation_alias=AliasChoices("capacity", "cap", "capacity_mbps", "bandwidth"),
            description="Capacity in integer Mbps.",
        ),
    ]


class TunnelIn(BaseModel):
    """A tunnel with a current path and a target path sharing the same endpoints."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    tunnel_id: Annotated[
        str,
        Field(
            min_length=1,
            validation_alias=AliasChoices("tunnel_id", "id", "name", "tunnel"),
            description="Unique tunnel identifier.",
        ),
    ]
    bandwidth: Annotated[
        int,
        Field(strict=True, ge=1, description="Tunnel bandwidth in integer Mbps."),
    ]
    current_path: Annotated[
        list[str],
        Field(
            min_length=1,
            validation_alias=AliasChoices(
                "current_path", "path", "old_path", "existing_path", "from_path"
            ),
            description="Ordered list of directed link ids currently in use.",
        ),
    ]
    target_path: Annotated[
        list[str],
        Field(
            min_length=1,
            validation_alias=AliasChoices(
                "target_path", "new_path", "planned_path", "to_path", "destination_path"
            ),
            description="Ordered list of directed link ids to migrate onto.",
        ),
    ]


class RehearsalIn(BaseModel):
    """Scenario submitted to create a rehearsal."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={"example": EXAMPLE_SCENARIO},
    )

    links: Annotated[
        list[LinkIn],
        Field(
            min_length=0,
            validation_alias=AliasChoices("links", "edges", "link_specs", "topology", "network"),
            description="Directed links with capacities.",
        ),
    ]
    tunnels: Annotated[
        list[TunnelIn],
        Field(
            validation_alias=AliasChoices("tunnels", "circuits", "tunnel_specs"),
            description="Between 2 and 10 tunnels to migrate.",
        ),
    ]


class ReserveIn(BaseModel):
    """Optional body for creating a reservation."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={"example": {"tunnel_id": "T1"}},
    )

    tunnel_id: Annotated[
        str | None,
        Field(
            default=None,
            validation_alias=AliasChoices("tunnel_id", "id", "tunnel"),
            description="Tunnel to reserve; defaults to the next tunnel in the order.",
        ),
    ]


# ------------------------------------------------------------- burst demands
class BurstDemandItemIn(BaseModel):
    """One tunnel's requested extra bandwidth, as a list element."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    tunnel_id: Annotated[
        str,
        Field(
            min_length=1,
            validation_alias=AliasChoices("tunnel_id", "id", "tunnel"),
            description="Tunnel the burst demand applies to.",
        ),
    ]
    demand: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            validation_alias=AliasChoices(
                "demand", "burst", "burst_demand", "bandwidth", "mbps", "amount"
            ),
            description="Extra bandwidth wanted, non-negative integer Mbps.",
        ),
    ]


class BurstDemandsIn(BaseModel):
    """Whole-group replacement of the burst demands for a rehearsal.

    Accepted shapes (all equivalent)::

        {"T1": 5, "T2": 0}                       # bare tunnel -> Mbps map
        {"demands": {"T1": 5}}                   # wrapped map
        {"demands": [{"tunnel_id": "T1", "demand": 5}]}

    Tunnels omitted from the map are written back as 0.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    demands: Annotated[
        dict[str, int],
        Field(
            default_factory=dict,
            validation_alias=AliasChoices(
                "demands", "burst_demands", "requests", "items"
            ),
            description="Map of tunnel id to extra bandwidth in non-negative integer Mbps.",
        ),
    ]

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_shapes(cls, data: Any) -> Any:
        if isinstance(data, dict) and "demands" not in data and not any(
            key in data
            for key in ("burst_demands", "requests", "items")
        ):
            # A bare {"T1": 5, "T2": 0} map: every value must be a strict
            # integer (floats and booleans are refused, never coerced).
            if data and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in data.values()
            ):
                return {"demands": dict(data)}
            if data:
                raise ValueError(
                    "burst demands must be a map of tunnel id to a non-negative "
                    "integer Mbps value"
                )
        if isinstance(data, list):
            return {"demands": data}
        return data

    @field_validator("demands", mode="before")
    @classmethod
    def _normalise_list(cls, value: Any) -> Any:
        if isinstance(value, list):
            normalised: dict[str, int] = {}
            for item in value:
                parsed = BurstDemandItemIn.model_validate(item)
                # Later entries win, matching an explicit map's last-write feel.
                normalised[parsed.tunnel_id] = parsed.demand
            return normalised
        return value

    @field_validator("demands")
    @classmethod
    def _non_negative_integers(cls, value: dict[str, int]) -> dict[str, int]:
        for tunnel_id, demand in value.items():
            if isinstance(demand, bool) or not isinstance(demand, int) or demand < 0:
                raise ValueError(
                    f"burst demand for {tunnel_id!r} must be a non-negative integer Mbps"
                )
        return value

    def normalised(self) -> dict[str, int]:
        return dict(self.demands)


# --------------------------------------------------------------------- outputs
class FractionOut(BaseModel):
    """An exact rational number: integer when whole, otherwise ``n/d``."""

    model_config = ConfigDict(extra="ignore")

    value: int | str = Field(
        description="Exact quota as an integer or a canonical 'numerator/denominator' string."
    )
    numerator: int
    denominator: int = Field(ge=1)


class BurstQuotaOut(BaseModel):
    """One tunnel's registered burst demand and its allocated exact quota."""

    tunnel_id: str
    demand: int = Field(description="Registered extra bandwidth wanted (integer Mbps).")
    quota: FractionOut = Field(
        description="Max-min fair quota used on every link of the tunnel's active path."
    )
    active_path: list[str] = Field(
        description="Active path this quota was computed against."
    )


class BurstLinkOut(BaseModel):
    """Per-link burst figures, all exact fractions in Mbps."""

    link_id: str
    used: FractionOut = Field(description="Sum of burst quotas carried on this link.")
    available: FractionOut = Field(
        description="Spare capacity after base usage, any pending reservation and burst traffic."
    )


class BurstAllocationOut(BaseModel):
    """The whole-group burst allocation computed from one stored state."""

    demands: dict[str, int] = Field(
        description="Effective burst demand per tunnel (unregistered tunnels read 0)."
    )
    quotas: list[BurstQuotaOut]
    links: list[BurstLinkOut]


class OrderStepOut(BaseModel):
    sequence: int
    tunnel_id: str
    bandwidth: int


class RehearsalCreatedOut(BaseModel):
    rehearsal_id: str
    status: str
    created_at: str
    order: list[str] = Field(description="Tunnel ids in a safe migration order.")
    steps: list[OrderStepOut]
    tunnels_total: int
    links_total: int


class TunnelStatusOut(BaseModel):
    tunnel_id: str
    bandwidth: int
    moved: bool
    current_path: list[str] = Field(description="Path currently carrying traffic.")
    initial_path: list[str]
    target_path: list[str]
    burst_demand: int = Field(
        default=0, description="Registered extra bandwidth wanted in integer Mbps."
    )
    burst_quota: FractionOut | None = Field(
        default=None,
        description="Allocated burst quota for the active path; null when not computed.",
    )


class LinkUsageOut(BaseModel):
    link_id: str
    source: str | None = None
    target: str | None = None
    capacity: int
    used: int
    reserved: int
    available: int
    burst_used: FractionOut | None = Field(
        default=None, description="Burst quota sum carried on the link (exact fraction)."
    )
    burst_available: FractionOut | None = Field(
        default=None,
        description="Capacity left after base usage, reservation and burst traffic.",
    )


class PendingOut(BaseModel):
    reservation_id: str
    tunnel_id: str
    sequence: int
    reserved: dict[str, int]


class RehearsalViewOut(BaseModel):
    rehearsal_id: str
    status: str
    created_at: str
    order: list[str]
    remaining_order: list[str]
    next_tunnel_id: str | None
    tunnels_total: int
    tunnels_moved: int
    tunnels: list[TunnelStatusOut]
    links: list[LinkUsageOut]
    pending: PendingOut | None
    burst: BurstAllocationOut


class ReservationOut(BaseModel):
    rehearsal_id: str
    reservation_id: str
    tunnel_id: str
    sequence: int
    status: str
    reserved: dict[str, int] = Field(
        description="Bandwidth held per link; links shared with the current path are free."
    )
    current_path: list[str]
    target_path: list[str]
    next_tunnel_id: str | None
    burst: BurstAllocationOut = Field(
        description="Whole-group burst allocation after this write."
    )


class ResolutionOut(BaseModel):
    rehearsal_id: str
    reservation_id: str
    tunnel_id: str
    sequence: int
    status: str
    current_path: list[str] = Field(description="Path carrying traffic after this call.")
    released: dict[str, int] = Field(
        default_factory=dict,
        description="Old-path bandwidth released on links not used by the new path.",
    )
    rehearsal_status: str
    next_tunnel_id: str | None
    burst: BurstAllocationOut = Field(
        description="Whole-group burst allocation after this write."
    )


class BurstDemandsOut(BaseModel):
    """Response of a whole-group burst-demand replacement."""

    rehearsal_id: str
    burst: BurstAllocationOut
