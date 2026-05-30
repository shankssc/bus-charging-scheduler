# src/scheduler/objective.py
# Weighted scoring function for dispatch queue ordering.
# Called every time a charger becomes free and there are buses waiting.
# Lower score = higher priority = dispatched first.
#
# Score = w_individual * individual_term
#       + w_operator   * operator_term
#       + w_overall    * overall_term
#       + sum of rule score_modifiers
#
# All terms are negative (more suffering = more deserving = lower score)
# except overall_term (longer remaining trip = more urgent = lower score).
#
# Changing a weight:
#   Update the weights dict in the scenario YAML file.
#   Example — to double operator fairness:
#     weights:
#       individual: 1.0
#       operator: 2.0    ← change this value
#       overall: 1.0
#   No code changes required.

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models import World
    from src.scheduler.rules import Rule
    from src.scheduler.simulation import BusState


def _individual_term(
    bus_id: str,
    current_time: float,
    bus_states: dict[str, BusState],
) -> float:
    """
    Compensates buses that have accumulated more total wait time.
    More waiting = more negative = higher priority.

    individual_term = -(total historical wait + current queue wait)
    """
    state = bus_states[bus_id]

    # sum of all completed wait periods at previous stations
    historical_wait = sum(ce.wait_min for ce in state.charge_events)

    # current wait at this station (time since bus joined the queue)
    current_wait = (
        current_time - state.wait_start
        if state.wait_start is not None
        else 0.0
    )

    return -(historical_wait + current_wait)


def _operator_term(
    bus_id: str,
    bus_states: dict[str, BusState],
) -> float:
    """
    Compensates operators whose fleet is running behind on average.
    Higher fleet average wait = more negative = higher priority for all
    buses from that operator.

    operator_term = -(fleet average total wait)
    """
    operator = bus_states[bus_id].operator

    fleet_waits = [
        sum(ce.wait_min for ce in state.charge_events)
        for state in bus_states.values()
        if state.operator == operator
    ]

    if not fleet_waits:
        return 0.0

    fleet_avg = sum(fleet_waits) / len(fleet_waits)
    return -fleet_avg


def _overall_term(
    bus_id: str,
    current_time: float,
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    world: World,
) -> float:
    """
    Prioritizes buses with more remaining trip time.
    Longer remaining trip = more negative = higher priority.
    Acts as a proxy for network impact — a delayed bus with lots of ground
    left to cover affects more downstream schedule slots.

    overall_term = -(estimated remaining trip time)

    Estimated remaining trip time =
        remaining travel time through plan stops to destination
        + charge time at remaining stops
        (wait times excluded — not knowable at dispatch time)
    """
    state = bus_states[bus_id]
    plan = assigned_plans.get(bus_id, [])

    if not plan:
        return 0.0

    cum_dist = world.cumulative_distances(state.origin)
    speed = world.defaults.speed_kmph
    charge_duration = world.defaults.charge_duration_min

    # find remaining stops in plan (stops not yet charged at)
    charged_stations = {ce.station for ce in state.charge_events}
    # also exclude current station if charging hasn't started yet
    # (bus is in queue — this stop hasn't been completed)
    remaining_plan = [s for s in plan if s not in charged_stations]

    if not remaining_plan:
        # all planned stops done — just remaining travel to destination
        current_stop = (
            state.charge_events[-1].station
            if state.charge_events
            else state.origin
        )
        dist_remaining = (
            cum_dist[state.destination] - cum_dist[current_stop]
        )
        travel_remaining = (dist_remaining / speed) * 60.0
        return -travel_remaining

    # compute remaining travel + charge time through remaining stops
    stops_sequence = remaining_plan + [state.destination]
    prev_dist = cum_dist[remaining_plan[0]]  # start from first remaining stop

    remaining_time = 0.0
    for stop in stops_sequence[1:]:
        dist = cum_dist[stop] - prev_dist
        remaining_time += (dist / speed) * 60.0
        prev_dist = cum_dist[stop]

    # add charge time for each remaining charging stop (excluding destination)
    remaining_time += len(remaining_plan) * charge_duration

    return -remaining_time


def compute_score(
    bus_id: str,
    current_time: float,
    bus_states: dict[str, BusState],
    assigned_plans: dict[str, list[str]],
    weights: dict[str, float],
    world: World,
    rules: list[Rule],
) -> float:
    """
    Compute the full priority score for a bus waiting in a dispatch queue.
    Lower score = higher priority = dispatched first.

    Parameters:
        bus_id:         bus being scored
        current_time:   current simulation time in minutes from midnight
        bus_states:     full state dict for all buses
        assigned_plans: pre-assigned charging plans
        weights:        scenario weights {individual, operator, overall}
        world:          world model (for route/speed data)
        rules:          active Rule objects (contribute score_modifiers)
    """
    w_individual = weights.get("individual", 1.0)
    w_operator = weights.get("operator", 1.0)
    w_overall = weights.get("overall", 1.0)

    # core weighted terms
    score = (
        w_individual * _individual_term(bus_id, current_time, bus_states)
        + w_operator * _operator_term(bus_id, bus_states)
        + w_overall
        * _overall_term(bus_id, current_time, bus_states, assigned_plans, world)
    )

    # soft rule modifiers (e.g. priority_bus_rule, energy_cost_rule)
    for rule in rules:
        score += rule.score_modifier(bus_id, current_time, bus_states, world)

    return score
