# src/loader.py
# Parses world.yaml and scenario YAML files into typed model objects.
# Everything downstream works with models, never raw dicts.
#
# Design principles:
#   - All YAML parsing is isolated here — one place to fix if schema changes
#   - Validates required fields and raises clear errors on bad input
#   - Computes derived fields (departure_min) so the rest of the system
#     never has to parse time strings

from __future__ import annotations

from pathlib import Path

import yaml

from src.models import (
    Bus,
    Defaults,
    Operator,
    Route,
    RuleConfig,
    Scenario,
    Segment,
    Stop,
    TrafficEvent,
    World,
)

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_time_to_minutes(time_str: str) -> int:
    """
    Convert 'HH:MM' string to minutes from midnight.

    Examples:
        '19:00' → 1140
        '20:45' → 1245
        '00:30' → 30
    """
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{time_str}' — expected HH:MM")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 23) or not (0 <= minutes <= 59):
        raise ValueError(f"Time out of range: '{time_str}'")
    return hours * 60 + minutes


def minutes_to_time_str(minutes: int) -> str:
    """
    Convert minutes from midnight back to 'HH:MM' string.
    Handles times past midnight (e.g. 1500 min = '01:00' next day).

    Examples:
        1140 → '19:00'
        1245 → '20:45'
        1500 → '01:00'
    """
    minutes = minutes % (24 * 60)  # wrap past midnight
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# World loader
# ---------------------------------------------------------------------------


def load_world(path: str | Path) -> World:
    """
    Parse world.yaml into a typed World object.
    Raises ValueError on missing required fields.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)

    # --- defaults ---
    d = raw["defaults"]
    defaults = Defaults(
        speed_kmph=float(d["speed_kmph"]),
        battery_range_km=float(d["battery_range_km"]),
        charge_duration_min=float(d["charge_duration_min"]),
        cooldown_min=float(d["cooldown_min"]),
    )

    # --- stops ---
    stops: list[Stop] = []
    for s in raw["route"]["stops"]:
        stops.append(
            Stop(
                id=s["id"],
                kind=s["kind"],
                chargers=int(s.get("chargers", 1)),
                charge_duration_min=(
                    float(s["charge_duration_min"])
                    if "charge_duration_min" in s
                    else None
                ),
                cooldown_min=(
                    float(s["cooldown_min"]) if "cooldown_min" in s else None
                ),
            )
        )

    # --- segments ---
    segments: list[Segment] = []
    for seg in raw["route"]["segments"]:
        traffic_events: list[TrafficEvent] = []
        for te in seg.get("traffic_events", []):
            traffic_events.append(
                TrafficEvent(
                    reason=te["reason"],
                    speed_kmph=float(te["speed_kmph"]),
                    from_time=te["from_time"],
                    to_time=te["to_time"],
                )
            )
        segments.append(
            Segment(
                from_stop=seg["from"],
                to_stop=seg["to"],
                distance_km=float(seg["distance_km"]),
                speed_kmph=(float(seg["speed_kmph"])
                            if "speed_kmph" in seg else None),
                traffic_events=traffic_events,
            )
        )

    route = Route(
        id=raw["route"]["id"],
        stops=stops,
        segments=segments,
    )

    # --- operators ---
    operators: list[Operator] = []
    for op in raw["operators"]:
        operators.append(
            Operator(
                id=op["id"],
                priority_tier=op.get("priority_tier", "standard"),
                max_wait_min=(
                    float(op["max_wait_min"])
                    if op.get("max_wait_min") is not None
                    else None
                ),
            )
        )

    # --- rules ---
    rules: list[RuleConfig] = []
    for r in raw.get("rules", []):
        rules.append(
            RuleConfig(
                id=r["id"],
                kind=r["kind"],
                enabled=bool(r["enabled"]),
                description=r.get("description", ""),
            )
        )

    # --- objective defaults ---
    obj = raw.get("objective_defaults", {})
    objective_defaults = {
        "individual": float(obj.get("individual", 1.0)),
        "operator": float(obj.get("operator", 1.0)),
        "overall": float(obj.get("overall", 1.0)),
    }

    return World(
        world_id=raw["world_id"],
        description=raw.get("description", ""),
        defaults=defaults,
        route=route,
        operators=operators,
        rules=rules,
        objective_defaults=objective_defaults,
    )


# ---------------------------------------------------------------------------
# Scenario loader
# ---------------------------------------------------------------------------


def load_scenario(path: str | Path) -> Scenario:
    """
    Parse a scenario YAML file into a typed Scenario object.
    Also computes departure_min for each bus.
    Raises ValueError on missing required fields.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)

    # --- weights ---
    w = raw.get("weights", {})
    weights = {
        "individual": float(w.get("individual", 1.0)),
        "operator": float(w.get("operator", 1.0)),
        "overall": float(w.get("overall", 1.0)),
    }

    # --- buses ---
    buses: list[Bus] = []
    for b in raw["buses"]:
        departure_str = b["departure"]
        departure_min = parse_time_to_minutes(departure_str)
        buses.append(
            Bus(
                id=b["id"],
                operator=b["operator"],
                origin=b["origin"],
                destination=b["destination"],
                departure=departure_str,
                departure_min=departure_min,
                battery_range_km=(
                    float(b["battery_range_km"]
                          ) if "battery_range_km" in b else None
                ),
                charge_duration_min=(
                    float(b["charge_duration_min"])
                    if "charge_duration_min" in b
                    else None
                ),
                priority=bool(b.get("priority", False)),
            )
        )

    # --- rule overrides ---
    rule_overrides: dict[str, bool] = {}
    for rule_id, enabled in raw.get("rule_overrides", {}).items():
        rule_overrides[str(rule_id)] = bool(enabled)

    # --- segment overrides ---
    segment_overrides: list[dict[str, object]] = []
    for override in raw.get("segment_overrides", []):
        segment_overrides.append(dict(override))

    return Scenario(
        scenario_id=raw["scenario_id"],
        description=raw.get("description", ""),
        world_id=raw["world_id"],
        weights=weights,
        buses=buses,
        rule_overrides=rule_overrides,
        segment_overrides=segment_overrides,
    )


# ---------------------------------------------------------------------------
# Convenience: load all scenarios from a directory
# ---------------------------------------------------------------------------


def load_all_scenarios(scenarios_dir: str | Path) -> list[Scenario]:
    """
    Load all scenario YAML files from a directory, sorted by filename.
    Returns a list of Scenario objects in file-sorted order.
    """
    scenarios_path = Path(scenarios_dir)
    scenario_files = sorted(scenarios_path.glob("scenario_*.yaml"))

    if not scenario_files:
        raise FileNotFoundError(
            f"No scenario files found in '{scenarios_dir}'. "
            "Expected files matching 'scenario_*.yaml'."
        )

    return [load_scenario(f) for f in scenario_files]


# ---------------------------------------------------------------------------
# Effective value helpers
# (bus-level overrides > world defaults)
# ---------------------------------------------------------------------------


def effective_battery_range(bus: Bus, world: World) -> float:
    """Resolve battery range — bus override if present, else world default."""
    return (
        bus.battery_range_km
        if bus.battery_range_km is not None
        else world.defaults.battery_range_km
    )


def effective_charge_duration(bus: Bus, world: World, stop: Stop) -> float:
    """
    Resolve charge duration with full override chain:
    bus override > stop override > world default.
    """
    if bus.charge_duration_min is not None:
        return bus.charge_duration_min
    if stop.charge_duration_min is not None:
        return stop.charge_duration_min
    return world.defaults.charge_duration_min
