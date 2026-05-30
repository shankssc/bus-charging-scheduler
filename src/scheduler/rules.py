# src/scheduler/rules.py
# Rule base class and all rule implementations.
# Each rule is either a hard constraint (eligibility gate) or a soft rule
# (score modifier). The simulation engine calls these — it never contains
# rule logic itself.
#
# Adding a new rule:
#   1. Create a class inheriting from Rule
#   2. Override is_eligible() for hard constraints
#   3. Override score_modifier() for soft objectives
#   4. Register it in build_active_rules()
#   5. Add an entry to world.yaml rules list
#   Zero changes to the simulation engine.
#
# Example — adding a DriverShiftRule:
#   class DriverShiftRule(Rule):
#       def __init__(self, shift_end_min: int):
#           self.shift_end_min = shift_end_min
#       def is_eligible(self, bus_id, time, bus_states, world):
#           return time < self.shift_end_min
#   Then in build_active_rules():
#       if _rule_enabled(world, scenario, 'driver_shift_rule'):
#           rules.append(DriverShiftRule(shift_end_min=1380))  # 23:00

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models import Scenario, World
    from src.scheduler.simulation import BusState


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Rule(ABC):
    """
    Base class for all scheduling rules.

    Hard rules override is_eligible() — returning False blocks a bus
    from being dispatched regardless of its score.

    Soft rules override score_modifier() — returning a non-zero float
    adjusts the bus's priority score. Negative = higher priority.
    """

    @property
    @abstractmethod
    def rule_id(self) -> str:
        """Unique identifier matching world.yaml rules list."""
        ...

    def is_eligible(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> bool:
        """
        Hard constraint gate.
        Return False to prevent this bus from being dispatched right now.
        Default: always eligible.
        """
        return True

    def score_modifier(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> float:
        """
        Soft objective contribution.
        Return a float added to this bus's priority score.
        Negative = higher priority (goes sooner).
        Default: no modification.
        """
        return 0.0


# ---------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------


class RangeRule(Rule):
    """
    Hard constraint: bus must never exceed battery_range_km between charges.
    Enforced during plan generation and plan validation — not at dispatch time.
    Included here for completeness and documentation; is_eligible always True
    because range violations are caught before simulation starts.
    """

    @property
    def rule_id(self) -> str:
        return "range_rule"


class SingleChargerRule(Rule):
    """
    Hard constraint: at most N buses charging simultaneously at a station
    (N = station charger count). Enforced by the simulation's charger slot
    management — not via is_eligible. Included for documentation.
    """

    @property
    def rule_id(self) -> str:
        return "single_charger_rule"


class RouteOrderRule(Rule):
    """
    Hard constraint: bus visits stations in route order, no backtracking.
    Enforced during plan generation — not at dispatch time.
    Included for documentation.
    """

    @property
    def rule_id(self) -> str:
        return "route_order_rule"


class PriorityBusRule(Rule):
    """
    Soft rule: buses flagged priority=True jump to front of queue.
    Applies a large negative score modifier so priority buses always
    beat non-priority buses regardless of other score terms.
    """

    PRIORITY_BONUS = -1_000_000.0  # dominates all other score terms

    @property
    def rule_id(self) -> str:
        return "priority_bus_rule"

    def score_modifier(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> float:
        state = bus_states.get(bus_id)
        if state is not None and state.priority:
            return self.PRIORITY_BONUS
        return 0.0


class DriverShiftRule(Rule):
    """
    Hard rule: bus cannot begin charging after shift_end_min.
    Prevents a bus from joining a queue if dispatching it would start
    charging past the driver's shift end time.

    Currently disabled (enabled: false in world.yaml).
    Enable by setting enabled: true and configuring shift_end_min.
    """

    def __init__(self, shift_end_min: int = 1380) -> None:  # default 23:00
        self.shift_end_min = shift_end_min

    @property
    def rule_id(self) -> str:
        return "driver_shift_rule"

    def is_eligible(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> bool:
        return time < self.shift_end_min


class EnergyCostRule(Rule):
    """
    Soft rule: penalizes charging during peak electricity cost windows.
    Higher cost window = positive score modifier = lower priority = deferred
    if other buses are willing to charge outside the peak window.

    Currently disabled (enabled: false in world.yaml).
    Enable by adding cost_windows to station data in world.yaml.

    Example future world.yaml station entry:
        - id: B
          kind: charger
          chargers: 1
          cost_windows:
            - from_time: "22:00"
              to_time: "23:00"
              cost_multiplier: 2.0
    """

    PEAK_PENALTY = 50.0  # added to score during peak window

    @property
    def rule_id(self) -> str:
        return "energy_cost_rule"

    def score_modifier(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> float:
        # placeholder — full implementation reads cost_windows from station data
        # and checks if current time falls within a peak window
        # returning 0.0 until energy_cost_rule is enabled
        return 0.0


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


def _rule_enabled(
    world: World,
    scenario: Scenario,
    rule_id: str,
) -> bool:
    """
    Determine if a rule is enabled, respecting scenario overrides.
    Scenario rule_overrides take precedence over world defaults.
    """
    # check scenario override first
    if rule_id in scenario.rule_overrides:
        return scenario.rule_overrides[rule_id]

    # fall back to world default
    for rule_config in world.rules:
        if rule_config.id == rule_id:
            return rule_config.enabled

    # unknown rule — default to disabled
    return False


def build_active_rules(
    world: World,
    scenario: Scenario,
) -> list[Rule]:
    """
    Build the list of active Rule objects for a scenario run.
    Rules are enabled/disabled via world.yaml and scenario rule_overrides.

    To add a new rule:
      1. Implement the Rule subclass above
      2. Add a conditional block here
      3. Add the rule entry to world.yaml
    """
    rules: list[Rule] = []

    # hard rules — structural constraints, always active
    # (range, single_charger, route_order enforced in plan generation/simulation)
    rules.append(RangeRule())
    rules.append(SingleChargerRule())
    rules.append(RouteOrderRule())

    # soft rules — enabled via world.yaml or scenario overrides
    if _rule_enabled(world, scenario, "priority_bus_rule"):
        rules.append(PriorityBusRule())

    if _rule_enabled(world, scenario, "driver_shift_rule"):
        rules.append(DriverShiftRule())

    if _rule_enabled(world, scenario, "energy_cost_rule"):
        rules.append(EnergyCostRule())

    return rules
