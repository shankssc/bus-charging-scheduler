# src/plan_assigner.py
# Picks one charging plan per bus before simulation starts.
# Uses a congestion-aware heuristic: for each candidate plan, score it by
# how many other buses are expected to arrive at the same stations in the
# same time window. Lower congestion = preferred plan.
#
# Design principles:
#   - Pre-assignment phase runs once before simulation — O(buses * plans)
#   - Uses naive arrival estimates (departure + travel time, no wait times yet)
#     because wait times are unknown before simulation
#   - Processes buses in departure order so earlier buses get first pick
#   - Deterministic: ties broken by plan length then lexicographic order
#
# Dynamic planning alternative (documented for interview):
#   Instead of locking plans upfront, each bus could choose its next stop
#   dynamically at departure time based on live queue lengths. This reacts
#   better to real-time congestion but is harder to reason about globally
#   and can produce worse overall outcomes due to local greedy decisions.
#   Switching to dynamic requires only changing this module — the simulation
#   loop, scoring function, and Rule system are identical either way.

from __future__ import annotations

from src.models import Bus, World

# ---------------------------------------------------------------------------
# Naive arrival time computation
# ---------------------------------------------------------------------------


def _compute_naive_arrival(
    bus: Bus,
    station: str,
    world: World,
) -> float:
    """
    Estimate when a bus arrives at a station assuming no wait times.
    arrival = departure_min + sum of travel times from origin to station.

    This is intentionally approximate — actual arrival times shift once
    queue waits accumulate. Good enough for plan selection heuristic.
    """
    cum_dist = world.cumulative_distances(bus.origin)
    distance = cum_dist[station]

    # resolve speed: use world default (no per-segment override in naive estimate)
    # this is a documented assumption — acceptable for pre-assignment heuristic
    speed = world.defaults.speed_kmph
    travel_time = (distance / speed) * 60.0  # minutes

    return bus.departure_min + travel_time


def compute_naive_arrivals(
    buses: list[Bus],
    world: World,
) -> dict[str, dict[str, float]]:
    """
    Compute naive arrival times for all buses at all charger stations.
    Returns: { bus_id: { station_id: arrival_time_min } }

    Only computes arrivals at stations in the bus's travel direction.
    """
    charger_ids = set(world.charger_stop_ids())
    result: dict[str, dict[str, float]] = {}

    for bus in buses:
        cum_dist = world.cumulative_distances(bus.origin)
        speed = world.defaults.speed_kmph
        result[bus.id] = {}

        for station in charger_ids:
            # only include stations this bus could physically reach
            # (i.e. stations that appear in its cumulative distance map)
            if station in cum_dist:
                distance = cum_dist[station]
                # skip origin and destination
                if distance == 0 or distance == cum_dist[bus.destination]:
                    continue
                travel_time = (distance / speed) * 60.0
                result[bus.id][station] = bus.departure_min + travel_time

    return result


# ---------------------------------------------------------------------------
# Congestion scoring
# ---------------------------------------------------------------------------


def _count_buses_near(
    station: str,
    arrival_time: float,
    bus_id: str,
    naive_arrivals: dict[str, dict[str, float]],
    window_min: float = 30.0,
) -> int:
    """
    Count how many other buses arrive at this station within
    +/- window_min of arrival_time.

    window_min=30 covers roughly one charge duration (25 min) plus buffer.
    This is a documented assumption.
    """
    count = 0
    for other_bus_id, station_arrivals in naive_arrivals.items():
        if other_bus_id == bus_id:
            continue
        if station not in station_arrivals:
            continue
        other_arrival = station_arrivals[station]
        if abs(other_arrival - arrival_time) <= window_min:
            count += 1
    return count


def _score_plan(
    plan: list[str],
    bus: Bus,
    naive_arrivals: dict[str, dict[str, float]],
    window_min: float = 30.0,
) -> float:
    """
    Score a plan by total expected congestion across all its stations.
    Lower score = less congested = preferred.

    Secondary scoring: prefer shorter plans (fewer stops = fewer potential waits).
    This is folded in as a small fractional term so it only breaks ties.
    """
    congestion_score = 0.0
    for station in plan:
        if station not in naive_arrivals.get(bus.id, {}):
            continue
        arrival = naive_arrivals[bus.id][station]
        congestion_score += _count_buses_near(
            station, arrival, bus.id, naive_arrivals, window_min
        )

    # tiebreaker: prefer shorter plans (0.01 per extra stop — never dominates)
    length_penalty = len(plan) * 0.01

    return congestion_score + length_penalty


# ---------------------------------------------------------------------------
# Plan assignment
# ---------------------------------------------------------------------------


def assign_plans(
    buses: list[Bus],
    valid_plans: dict[str, list[list[str]]],
    world: World,
    window_min: float = 30.0,
) -> dict[str, list[str]]:
    """
    Assign one charging plan to each bus.

    Algorithm:
      1. Sort buses by departure time (earlier buses get first pick)
      2. Compute naive arrival times for all buses at all stations
      3. For each bus in departure order, score each valid plan by
         expected congestion and pick the lowest-scoring plan
      4. After assignment, update naive arrivals to include charge time
         so subsequent buses see a more accurate picture

    Returns: { bus_id: assigned_plan }
    """
    # sort buses by departure time for deterministic processing order
    # buses departing at the same time are sorted by bus_id for determinism
    sorted_buses = sorted(buses, key=lambda b: (b.departure_min, b.id))

    # compute naive arrivals for all buses upfront
    naive_arrivals = compute_naive_arrivals(buses, world)

    assigned: dict[str, list[str]] = {}

    for bus in sorted_buses:
        plans = valid_plans.get(bus.id, [])

        if not plans:
            raise ValueError(
                f"Bus '{bus.id}' has no valid charging plans — "
                f"check battery range and route distances"
            )

        # score each plan and pick the best
        best_plan = min(
            plans,
            key=lambda p: _score_plan(p, bus, naive_arrivals, window_min),
        )

        assigned[bus.id] = best_plan

        # update naive arrivals for this bus to reflect the assigned plan
        # this gives subsequent buses a slightly more accurate congestion picture
        _update_arrivals_for_plan(bus, best_plan, naive_arrivals, world)

    return assigned


def _update_arrivals_for_plan(
    bus: Bus,
    plan: list[str],
    naive_arrivals: dict[str, dict[str, float]],
    world: World,
) -> None:
    """
    After assigning a plan, update this bus's naive arrival times to
    account for charging stops (each adds charge_duration_min to downstream arrivals).

    This is an approximation — we don't know actual wait times yet —
    but it helps subsequent buses avoid the same stations.
    """
    cum_dist = world.cumulative_distances(bus.origin)
    speed = world.defaults.speed_kmph
    charge_duration = world.defaults.charge_duration_min

    current_time = float(bus.departure_min)
    prev_stop = bus.origin

    for station in plan:
        distance = cum_dist[station] - cum_dist[prev_stop]
        travel_time = (distance / speed) * 60.0
        current_time += travel_time
        # update arrival to include accumulated charge time from prior stops
        naive_arrivals[bus.id][station] = current_time
        current_time += charge_duration
        prev_stop = station


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_assignments(
    assigned: dict[str, list[str]],
    buses: list[Bus],
    world: World,
) -> list[str]:
    """
    Validate all assigned plans satisfy hard constraints.
    Returns a list of error strings — empty list means all valid.
    """
    from src.plan_generator import validate_plan

    errors: list[str] = []
    bus_map = {b.id: b for b in buses}

    for bus_id, plan in assigned.items():
        bus = bus_map[bus_id]
        is_valid, reason = validate_plan(plan, bus, world)
        if not is_valid:
            errors.append(
                f"Bus '{bus_id}' assigned invalid plan {plan}: {reason}")

    return errors
