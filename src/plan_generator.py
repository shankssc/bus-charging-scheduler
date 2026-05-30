# src/plan_generator.py
# Generates all valid charging plans for a bus given the world constraints.
# A plan is an ordered list of charging station IDs the bus will stop at.
#
# Design principles:
#   - Purely functional — no side effects, no state mutation
#   - Derived entirely from data — no hardcoded station names or distances
#   - Works for any route direction by using cumulative distances from origin
#   - Single responsibility — only generates plans, never assigns them

from __future__ import annotations

from src.loader import effective_battery_range
from src.models import Bus, World


def _stops_in_travel_order(bus: Bus, world: World) -> list[str]:
    """
    Return all stop IDs in the order this bus will encounter them.

    BLR→KCH bus: [bengaluru, A, B, C, D, kochi]
    KCH→BLR bus: [kochi, D, C, B, A, bengaluru]
    """
    stop_ids = world.stop_ids()
    origin_idx = stop_ids.index(bus.origin)

    if origin_idx == 0:
        # forward direction
        return stop_ids
    elif origin_idx == len(stop_ids) - 1:
        # reverse direction
        return list(reversed(stop_ids))
    else:
        raise ValueError(
            f"Bus '{bus.id}' origin '{bus.origin}' must be a depot "
            f"(first or last stop in route)"
        )


def _charger_stops_in_travel_order(bus: Bus, world: World) -> list[str]:
    """
    Return only the charging station IDs in the order this bus encounters them.
    Excludes depots.

    BLR→KCH: [A, B, C, D]
    KCH→BLR: [D, C, B, A]
    """
    all_stops = _stops_in_travel_order(bus, world)
    stop_map = {s.id: s for s in world.route.stops}
    return [s for s in all_stops if stop_map[s].kind == "charger"]


def generate_plans(bus: Bus, world: World) -> list[list[str]]:
    """
    Generate all valid charging plans for a bus.

    A plan is a list of charging station IDs (in travel order) such that
    every consecutive hop — including origin→first_stop and last_stop→destination
    — is within the bus's battery range.

    Returns a list of plans, each plan being an ordered list of station IDs.
    Plans are sorted by length (fewest stops first) then lexicographically,
    so the simplest valid plan is always first.

    Examples for BLR→KCH (range=240km):
        ['A', 'C']
        ['B', 'C']
        ['B', 'D']
        ['A', 'B', 'C']
        ... etc

    Examples for KCH→BLR (range=240km):
        ['D', 'B']
        ['C', 'B']
        ['C', 'A']
        ... etc
    """
    battery_range = effective_battery_range(bus, world)
    cum_dist = world.cumulative_distances(bus.origin)
    charger_stops = _charger_stops_in_travel_order(bus, world)

    all_plans: list[list[str]] = []

    def _search(current_stop: str, plan_so_far: list[str]) -> None:
        """
        Recursively extend the plan from current_stop.
        current_stop is either the origin or the last charging stop added.
        """
        current_dist = cum_dist[current_stop]

        for next_stop in charger_stops:
            next_dist = cum_dist[next_stop]

            # only consider stops further along the route than current
            if next_dist <= current_dist:
                continue

            hop = next_dist - current_dist

            # hard constraint: hop must be within battery range
            if hop > battery_range:
                # stops are ordered by distance — no point checking further
                break

            # check if destination is reachable from next_stop
            destination_dist = cum_dist[bus.destination]
            remaining = destination_dist - next_dist

            if remaining <= battery_range:
                # destination is reachable from here — this is a valid plan
                all_plans.append(plan_so_far + [next_stop])

            # keep searching deeper regardless — there may be valid
            # extensions even if destination is already reachable
            _search(next_stop, plan_so_far + [next_stop])

    _search(bus.origin, [])

    # sort: fewest stops first, then lexicographically for determinism
    all_plans.sort(key=lambda p: (len(p), p))

    return all_plans


def validate_plan(
    plan: list[str],
    bus: Bus,
    world: World,
) -> tuple[bool, str]:
    """
    Validate that a specific plan satisfies all hard constraints.
    Returns (is_valid, reason).

    Used as a sanity check after plan assignment.
    """
    battery_range = effective_battery_range(bus, world)
    cum_dist = world.cumulative_distances(bus.origin)
    charger_stops = _charger_stops_in_travel_order(bus, world)

    if not plan:
        return False, "Plan is empty — bus needs at least one charging stop"

    # verify stops are in travel order
    charger_positions = {s: i for i, s in enumerate(charger_stops)}
    for stop in plan:
        if stop not in charger_positions:
            return False, f"Stop '{stop}' is not a charger on this bus's route"

    positions = [charger_positions[s] for s in plan]
    if positions != sorted(positions):
        return False, f"Plan stops are not in travel order: {plan}"

    # check every hop
    all_stops = [bus.origin] + plan + [bus.destination]
    for i in range(len(all_stops) - 1):
        frm = all_stops[i]
        to = all_stops[i + 1]
        hop = cum_dist[to] - cum_dist[frm]
        if hop > battery_range:
            return (
                False,
                f"Hop '{frm}'→'{to}' is {hop:.0f} km — exceeds range {battery_range:.0f} km",
            )

    return True, "ok"


def generate_plans_for_all_buses(
    buses: list[Bus],
    world: World,
) -> dict[str, list[list[str]]]:
    """
    Generate valid plans for every bus in a scenario.
    Returns a dict mapping bus_id → list of valid plans.
    """
    return {bus.id: generate_plans(bus, world) for bus in buses}
