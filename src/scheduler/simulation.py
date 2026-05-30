# src/scheduler/simulation.py
# Event-driven simulation loop.
# Processes events in time order, resolving charger queue conflicts
# using the weighted scoring function and active Rule objects.
#
# Event types:
#   BUS_ARRIVES        — bus reaches its next planned charging station
#   CHARGING_COMPLETE  — bus finishes charging, departs station
#   CHARGER_FREE       — cooldown period ends, next bus can be dispatched
#
# Adding a new event type:
#   1. Add a constant below
#   2. Add a handler function
#   3. Add a branch in run_simulation()
#   Zero changes to existing handlers.

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.models import (
    BusResult,
    ChargeEvent,
    Scenario,
    SchedulerResult,
    StationResult,
    StationSlotResult,
    World,
)
from src.scheduler.objective import compute_score
from src.scheduler.rules import Rule

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

BUS_ARRIVES = "BUS_ARRIVES"
CHARGING_COMPLETE = "CHARGING_COMPLETE"
CHARGER_FREE = "CHARGER_FREE"

# ---------------------------------------------------------------------------
# Mutable simulation state
# ---------------------------------------------------------------------------


@dataclass
class ChargeEventMutable:
    """In-progress charge event — converted to immutable ChargeEvent on completion."""

    station: str
    arrive_min: float
    wait_min: float = 0.0
    charge_start_min: float = 0.0
    charge_end_min: float = 0.0


@dataclass
class BusState:
    """Mutable state for a single bus during simulation."""

    bus_id: str
    operator: str
    origin: str
    destination: str
    departure_min: int
    priority: bool = False

    # completed charge events (immutable snapshots)
    charge_events: list[ChargeEventMutable] = field(default_factory=list)

    # current queue state
    wait_start: float | None = None  # time bus joined current queue
    current_station: str | None = None  # station bus is currently at/waiting at

    # final result
    arrival_min: float | None = None  # when bus reached destination


@dataclass
class ChargerSlot:
    """One charger slot at a station."""

    slot_index: int
    free_at: float  # time this slot becomes available
    queue: list[str] = field(default_factory=list)  # bus_ids waiting
    # dispatch log: (charge_start, charge_end, bus_id, wait_min)
    dispatch_log: list[tuple[float, float, str, float]
                       ] = field(default_factory=list)


@dataclass
class StationState:
    """All charger slots at a station."""

    station_id: str
    slots: list[ChargerSlot]


# ---------------------------------------------------------------------------
# Travel time computation
# ---------------------------------------------------------------------------


def _travel_time(
    from_stop: str,
    to_stop: str,
    depart_time: float,
    world: World,
) -> float:
    """
    Compute travel time in minutes from from_stop to to_stop.
    Accounts for per-segment speed overrides and traffic events.

    depart_time is used to check traffic event windows.
    """
    # build ordered stop list for this direction
    stop_ids = world.stop_ids()
    from_idx = stop_ids.index(from_stop)
    to_idx = stop_ids.index(to_stop)

    if from_idx < to_idx:
        # forward direction
        leg_stops = stop_ids[from_idx: to_idx + 1]
        direction = "forward"
    else:
        # reverse direction
        leg_stops = list(reversed(stop_ids[to_idx: from_idx + 1]))
        direction = "reverse"

    total_time = 0.0
    current_time = depart_time

    for i in range(len(leg_stops) - 1):
        frm = leg_stops[i]
        nxt = leg_stops[i + 1]

        # look up segment in canonical (forward) order
        if direction == "forward":
            seg = world.get_segment(frm, nxt)
        else:
            seg = world.get_segment(nxt, frm)

        # resolve speed — check traffic events first
        speed = _resolve_speed(seg, current_time, world)
        segment_time = (seg.distance_km / speed) * 60.0
        total_time += segment_time
        current_time += segment_time

    return total_time


def _resolve_speed(seg: object, current_time: float, world: World) -> float:
    """
    Resolve effective speed for a segment at a given time.
    Checks traffic events (temporary speed reductions) first,
    then segment override, then world default.
    """
    from src.models import Segment

    if not isinstance(seg, Segment):
        return world.defaults.speed_kmph

    # check traffic events
    for event in seg.traffic_events:
        event_start = _time_str_to_min(event.from_time)
        event_end = _time_str_to_min(event.to_time)
        if event_start <= current_time <= event_end:
            return event.speed_kmph

    # segment override
    if seg.speed_kmph is not None:
        return seg.speed_kmph

    # world default
    return world.defaults.speed_kmph


def _time_str_to_min(time_str: str) -> float:
    """Convert HH:MM to minutes from midnight."""
    h, m = time_str.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# Station state initialisation
# ---------------------------------------------------------------------------


def _init_station_states(world: World) -> dict[str, StationState]:
    """Create empty StationState for every charging station."""
    states: dict[str, StationState] = {}
    for stop in world.route.stops:
        if stop.kind == "charger":
            slots = [
                ChargerSlot(slot_index=i, free_at=0.0) for i in range(stop.chargers)
            ]
            states[stop.id] = StationState(station_id=stop.id, slots=slots)
    return states


def _init_bus_states(scenario: Scenario) -> dict[str, BusState]:
    """Create initial BusState for every bus in the scenario."""
    return {
        bus.id: BusState(
            bus_id=bus.id,
            operator=bus.operator,
            origin=bus.origin,
            destination=bus.destination,
            departure_min=bus.departure_min,
            priority=bus.priority,
        )
        for bus in scenario.buses
    }


# ---------------------------------------------------------------------------
# Core dispatch helpers
# ---------------------------------------------------------------------------


def _best_slot(station_state: StationState, current_time: float) -> ChargerSlot:
    """Return the slot with the earliest free_at time."""
    return min(station_state.slots, key=lambda s: s.free_at)


def _start_charging(
    time: float,
    bus_id: str,
    station_id: str,
    slot: ChargerSlot,
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    events: list[tuple[float, int, str, str, str | None]],
    world: World,
    event_counter: list[int],
) -> None:
    """
    Begin charging a bus at a slot. Schedules CHARGING_COMPLETE event.
    """
    state = bus_states[bus_id]
    # resolve charge duration using effective helper
    stop = world.get_stop(station_id)

    charge_duration = world.effective_charge_duration(stop)

    charge_end = time + charge_duration
    wait = time - state.wait_start if state.wait_start is not None else 0.0

    # record charge event start
    state.charge_events.append(
        ChargeEventMutable(
            station=station_id,
            arrive_min=state.wait_start if state.wait_start is not None else time,
            wait_min=wait,
            charge_start_min=time,
            charge_end_min=charge_end,
        )
    )

    # lock the slot
    slot.free_at = charge_end + world.effective_cooldown(stop)

    # log dispatch
    slot.dispatch_log.append((time, charge_end, bus_id, wait))

    # clear wait state
    state.wait_start = None

    # schedule completion
    event_counter[0] += 1
    heapq.heappush(
        events,
        (charge_end, event_counter[0], CHARGING_COMPLETE, bus_id, station_id),
    )


def _dispatch_next_from_queue(
    time: float,
    station_id: str,
    slot: ChargerSlot,
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    events: list[tuple[float, int, str, str, str | None]],
    weights: dict[str, float],
    world: World,
    rules: list[Rule],
    event_counter: list[int],
) -> None:
    """
    When a charger slot becomes free, score all waiting buses and
    dispatch the highest-priority (lowest-score) eligible bus.
    """
    if not slot.queue:
        return

    # filter to eligible buses (hard rules)
    eligible = [
        bus_id
        for bus_id in slot.queue
        if all(rule.is_eligible(bus_id, time, bus_states, world) for rule in rules)
    ]

    if not eligible:
        return

    # score eligible buses — lowest score wins
    scored = sorted(
        eligible,
        key=lambda bid: (
            compute_score(bid, time, bus_states, assigned_plans,
                          weights, world, rules),
            bid,  # tiebreaker: bus_id lexicographic
        ),
    )

    winner_id = scored[0]
    slot.queue.remove(winner_id)

    _start_charging(
        time,
        winner_id,
        station_id,
        slot,
        bus_states,
        assigned_plans,
        events,
        world,
        event_counter,
    )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_arrival(
    time: float,
    bus_id: str,
    station_id: str,
    station_states: dict[str, StationState],
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    events: list[tuple[float, int, str, str, str | None]],
    weights: dict[str, float],
    world: World,
    rules: list[Rule],
    event_counter: list[int],
) -> None:
    """Bus arrives at its next planned charging station."""
    state = bus_states[bus_id]
    state.current_station = station_id
    state.wait_start = time

    station = station_states[station_id]
    slot = _best_slot(station, time)

    if slot.free_at <= time:
        # charger free — start immediately
        _start_charging(
            time,
            bus_id,
            station_id,
            slot,
            bus_states,
            assigned_plans,
            events,
            world,
            event_counter,
        )
    else:
        # charger busy — join queue
        slot.queue.append(bus_id)


def _handle_charging_complete(
    time: float,
    bus_id: str,
    station_id: str,
    station_states: dict[str, StationState],
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    events: list[tuple[float, int, str, str, str | None]],
    weights: dict[str, float],
    world: World,
    rules: list[Rule],
    event_counter: list[int],
) -> None:
    """Bus finishes charging. Schedule its next stop or mark arrival."""
    state = bus_states[bus_id]
    plan = assigned_plans.get(bus_id, [])

    # find next stop in plan after this station
    try:
        current_idx = plan.index(station_id)
        next_stop = plan[current_idx + 1] if current_idx + \
            1 < len(plan) else None
    except ValueError:
        next_stop = None

    if next_stop is None:
        # no more charging stops — travel to destination
        travel = _travel_time(station_id, state.destination, time, world)
        state.arrival_min = time + travel
    else:
        # schedule arrival at next charging station
        travel = _travel_time(station_id, next_stop, time, world)
        arrival_time = time + travel
        event_counter[0] += 1
        heapq.heappush(
            events,
            (arrival_time, event_counter[0], BUS_ARRIVES, bus_id, next_stop),
        )

    # find the slot this bus was charging on and dispatch next from queue
    stop = world.get_stop(station_id)
    cooldown = world.effective_cooldown(stop)
    station = station_states[station_id]

    # find the slot that just completed (the one whose free_at = time + cooldown)
    for slot in station.slots:
        if abs(slot.free_at - (time + cooldown)) < 0.001:
            if cooldown > 0:
                # schedule CHARGER_FREE after cooldown
                event_counter[0] += 1
                heapq.heappush(
                    events,
                    (
                        time + cooldown,
                        event_counter[0],
                        CHARGER_FREE,
                        station_id,
                        None,
                    ),
                )
            else:
                # no cooldown — dispatch next immediately
                _dispatch_next_from_queue(
                    time,
                    station_id,
                    slot,
                    bus_states,
                    assigned_plans,
                    events,
                    weights,
                    world,
                    rules,
                    event_counter,
                )
            break


def _handle_charger_free(
    time: float,
    station_id: str,
    station_states: dict[str, StationState],
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    events: list[tuple[float, int, str, str, str | None]],
    weights: dict[str, float],
    world: World,
    rules: list[Rule],
    event_counter: list[int],
) -> None:
    """Cooldown period ended — dispatch next bus from queue."""
    station = station_states[station_id]
    for slot in station.slots:
        if abs(slot.free_at - time) < 0.001 and slot.queue:
            _dispatch_next_from_queue(
                time,
                station_id,
                slot,
                bus_states,
                assigned_plans,
                events,
                weights,
                world,
                rules,
                event_counter,
            )
            break


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------


def _build_results(
    scenario: Scenario,
    bus_states: dict[str, BusState],
    station_states: dict[str, StationState],
    assigned_plans: dict[str, list[str]],
) -> SchedulerResult:
    """Convert mutable simulation state into immutable SchedulerResult."""

    bus_results: list[BusResult] = []
    for bus in scenario.buses:
        state = bus_states[bus.id]
        charge_events = [
            ChargeEvent(
                station=ce.station,
                arrive_min=int(ce.arrive_min),
                wait_min=int(ce.wait_min),
                charge_start_min=int(ce.charge_start_min),
                charge_end_min=int(ce.charge_end_min),
            )
            for ce in state.charge_events
        ]
        total_wait = sum(ce.wait_min for ce in charge_events)
        arrival = int(
            state.arrival_min) if state.arrival_min is not None else -1
        trip_time = arrival - bus.departure_min if arrival >= 0 else -1

        bus_results.append(
            BusResult(
                bus_id=bus.id,
                operator=bus.operator,
                origin=bus.origin,
                destination=bus.destination,
                departure_min=bus.departure_min,
                charging_plan=assigned_plans.get(bus.id, []),
                charge_events=charge_events,
                arrival_min=arrival,
                total_wait_min=total_wait,
                total_trip_min=trip_time,
            )
        )

    station_results: list[StationResult] = []
    for station_id, ss in sorted(station_states.items()):
        slot_results = []
        for slot in ss.slots:
            events_for_slot = [
                ChargeEvent(
                    station=station_id,
                    arrive_min=int(log[0] - log[3]),  # charge_start - wait
                    wait_min=int(log[3]),
                    charge_start_min=int(log[0]),
                    charge_end_min=int(log[1]),
                )
                for log in slot.dispatch_log
            ]
            slot_results.append(
                StationSlotResult(
                    slot_index=slot.slot_index,
                    events=events_for_slot,
                )
            )
        station_results.append(StationResult(
            station_id=station_id, slots=slot_results))

    return SchedulerResult(
        scenario_id=scenario.scenario_id,
        bus_results=bus_results,
        station_results=station_results,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_simulation(
    scenario: Scenario,
    world: World,
    assigned_plans: dict[str, list[str]],
    weights: dict[str, float],
    rules: list[Rule],
) -> SchedulerResult:
    """
    Run the full event-driven simulation for a scenario.

    Returns a SchedulerResult with per-bus timelines and per-station
    dispatch orders.
    """
    # initialise state
    bus_states = _init_bus_states(scenario)
    station_states = _init_station_states(world)

    # event queue: (time, counter, event_type, primary_id, secondary_id)
    # counter is a monotonically increasing int to break time ties
    # deterministically (heapq compares tuples left-to-right)
    events: list[tuple[float, int, str, str, str | None]] = []
    event_counter = [0]  # mutable counter via list

    # seed initial events — each bus's first charging stop arrival
    for bus in scenario.buses:
        plan = assigned_plans.get(bus.id, [])
        if not plan:
            continue
        first_station = plan[0]
        travel = _travel_time(
            bus.origin, first_station, float(bus.departure_min), world
        )
        arrival_time = bus.departure_min + travel
        event_counter[0] += 1
        heapq.heappush(
            events,
            (arrival_time, event_counter[0],
             BUS_ARRIVES, bus.id, first_station),
        )

    # process events in time order
    while events:
        time, _, event_type, primary_id, secondary_id = heapq.heappop(events)

        if event_type == BUS_ARRIVES:
            assert secondary_id is not None
            _handle_arrival(
                time,
                primary_id,
                secondary_id,
                station_states,
                bus_states,
                assigned_plans,
                events,
                weights,
                world,
                rules,
                event_counter,
            )

        elif event_type == CHARGING_COMPLETE:
            assert secondary_id is not None
            _handle_charging_complete(
                time,
                primary_id,
                secondary_id,
                station_states,
                bus_states,
                assigned_plans,
                events,
                weights,
                world,
                rules,
                event_counter,
            )

        elif event_type == CHARGER_FREE:
            _handle_charger_free(
                time,
                primary_id,
                station_states,
                bus_states,
                assigned_plans,
                events,
                weights,
                world,
                rules,
                event_counter,
            )

    return _build_results(scenario, bus_states, station_states, assigned_plans)
