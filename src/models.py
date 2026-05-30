# src/models.py
# Dataclasses representing the parsed world and scenario.
# Every other module imports from here — no raw dicts passed around.
# All fields are typed so mypy catches mistakes early.

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# World models — loaded from world.yaml, shared across all scenarios
# ---------------------------------------------------------------------------


@dataclass
class TrafficEvent:
    """A temporary speed reduction on a segment (roadwork, accident, etc.)."""

    reason: str
    speed_kmph: float
    from_time: str  # HH:MM 24h — e.g. "20:00"
    to_time: str  # HH:MM 24h — e.g. "23:00"


@dataclass
class Segment:
    """A road segment between two consecutive stops."""

    from_stop: str
    to_stop: str
    distance_km: float
    speed_kmph: float | None = None  # None = inherit world default
    traffic_events: list[TrafficEvent] = field(default_factory=list)


@dataclass
class Stop:
    """
    A point on the route.
    kind='depot'   → fully charges buses before departure, not a scheduling resource.
    kind='charger' → scheduling resource, buses may stop here to recharge.
    """

    id: str
    kind: str  # 'depot' | 'charger'
    chargers: int = 1
    charge_duration_min: float | None = None  # None = inherit world default
    cooldown_min: float | None = None  # None = inherit world default


@dataclass
class Operator:
    """An operator running a fleet of buses."""

    id: str
    priority_tier: str = "standard"  # 'standard' | 'express' | 'priority'
    max_wait_min: float | None = None  # None = no SLA enforced


@dataclass
class RuleConfig:
    """A rule entry from the world registry."""

    id: str
    kind: str  # 'hard' | 'soft'
    enabled: bool
    description: str = ""


@dataclass
class Defaults:
    """Physical constants — world-level defaults, overrideable per segment or bus."""

    speed_kmph: float
    battery_range_km: float
    charge_duration_min: float
    cooldown_min: float


@dataclass
class Route:
    """The full route — ordered stops and the segments connecting them."""

    id: str
    stops: list[Stop]
    segments: list[Segment]


@dataclass
class World:
    """
    The physical world loaded from world.yaml.
    Shared across all scenarios — never changes between runs.
    """

    world_id: str
    description: str
    defaults: Defaults
    route: Route
    operators: list[Operator]
    rules: list[RuleConfig]
    objective_defaults: dict[str, float]

    # --- convenience helpers ---

    def stop_ids(self) -> list[str]:
        """Ordered list of all stop IDs along the route."""
        return [s.id for s in self.route.stops]

    def charger_stop_ids(self) -> list[str]:
        """Ordered list of charging station IDs only (excludes depots)."""
        return [s.id for s in self.route.stops if s.kind == "charger"]

    def get_stop(self, stop_id: str) -> Stop:
        """Look up a Stop by ID. Raises KeyError if not found."""
        for stop in self.route.stops:
            if stop.id == stop_id:
                return stop
        raise KeyError(f"Stop '{stop_id}' not found in route")

    def get_segment(self, from_stop: str, to_stop: str) -> Segment:
        """Look up a Segment by its endpoints. Raises KeyError if not found."""
        for seg in self.route.segments:
            if seg.from_stop == from_stop and seg.to_stop == to_stop:
                return seg
        raise KeyError(f"Segment '{from_stop}' → '{to_stop}' not found in route")

    def effective_speed(self, segment: Segment) -> float:
        """Resolve segment speed — use segment override if present, else world default."""
        return (
            segment.speed_kmph
            if segment.speed_kmph is not None
            else self.defaults.speed_kmph
        )

    def effective_charge_duration(self, stop: Stop) -> float:
        """Resolve charge duration — use stop override if present, else world default."""
        return (
            stop.charge_duration_min
            if stop.charge_duration_min is not None
            else self.defaults.charge_duration_min
        )

    def effective_cooldown(self, stop: Stop) -> float:
        """Resolve cooldown — use stop override if present, else world default."""
        return (
            stop.cooldown_min
            if stop.cooldown_min is not None
            else self.defaults.cooldown_min
        )

    def cumulative_distances(self, origin: str) -> dict[str, float]:
        """
        Compute cumulative distances from a given origin stop.
        Works for both directions — derives order from origin position in route.

        BLR→KCH: origin=bengaluru → distances increase forward
        KCH→BLR: origin=kochi    → distances increase in reverse
        """
        stop_ids = self.stop_ids()
        origin_idx = stop_ids.index(origin)

        # determine travel direction
        stops_in_order: list[str]
        if origin_idx == 0:
            # forward: bengaluru → kochi
            stops_in_order = stop_ids
        elif origin_idx == len(stop_ids) - 1:
            # reverse: kochi → bengaluru
            stops_in_order = list(reversed(stop_ids))
        else:
            raise ValueError(
                f"Origin '{origin}' must be a depot (first or last stop in route)"
            )

        cum: dict[str, float] = {}
        dist = 0.0
        for i, stop_id in enumerate(stops_in_order):
            cum[stop_id] = dist
            if i < len(stops_in_order) - 1:
                frm = stops_in_order[i]
                to = stops_in_order[i + 1]
                # segments are stored forward — look up in canonical order
                seg = (
                    self.get_segment(frm, to)
                    if origin_idx == 0
                    else self.get_segment(to, frm)
                )
                dist += seg.distance_km

        return cum


# ---------------------------------------------------------------------------
# Bus models — loaded from scenario YAML
# ---------------------------------------------------------------------------


@dataclass
class Bus:
    """A single bus in a scenario."""

    id: str
    operator: str
    origin: str
    destination: str
    departure: str  # HH:MM 24h string — e.g. "19:00"
    departure_min: int = 0  # departure in minutes from midnight (computed by loader)

    # optional per-bus overrides of world defaults
    battery_range_km: float | None = None  # None = use world default
    charge_duration_min: float | None = None  # None = use world default
    priority: bool = False  # True = priority bus rule applies


# ---------------------------------------------------------------------------
# Scenario model — loaded from scenarios/scenario_N.yaml
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """
    A single scenario — departure schedule + weights + optional overrides.
    References a world by world_id.
    """

    scenario_id: str
    description: str
    world_id: str
    weights: dict[str, float]
    buses: list[Bus]
    rule_overrides: dict[str, bool] = field(default_factory=dict)
    segment_overrides: list[dict[str, object]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Result models — produced by the scheduler, consumed by the UI
# ---------------------------------------------------------------------------


@dataclass
class ChargeEvent:
    """A single charging stop for a bus."""

    station: str
    arrive_min: int  # when bus arrived at station
    wait_min: int  # how long bus waited in queue
    charge_start_min: int  # when charging actually started
    charge_end_min: int  # when charging completed


@dataclass
class BusResult:
    """Full timeline for a single bus — output of the scheduler."""

    bus_id: str
    operator: str
    origin: str
    destination: str
    departure_min: int
    charging_plan: list[str]  # ordered list of station IDs used
    charge_events: list[ChargeEvent]
    arrival_min: int  # when bus reached its destination
    total_wait_min: int  # sum of all queue waits
    total_trip_min: int  # departure → arrival


@dataclass
class StationSlotResult:
    """Charging order at a single charger slot within a station."""

    slot_index: int
    events: list[ChargeEvent]  # in dispatch order


@dataclass
class StationResult:
    """All charger slots at a single station — output of the scheduler."""

    station_id: str
    slots: list[StationSlotResult]


@dataclass
class SchedulerResult:
    """Complete output of a scheduler run — fed directly to the Streamlit UI."""

    scenario_id: str
    bus_results: list[BusResult]
    station_results: list[StationResult]