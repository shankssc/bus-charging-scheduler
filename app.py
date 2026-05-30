# app.py
# Streamlit UI for the Bus Charging Scheduler.
# Single entry point — run with: streamlit run app.py
#
# Layout:
#   1. Scenario dropdown
#   2. Scenario input table (raw data)
#   3. Per-bus timetable
#   4. Per-station charging order

from __future__ import annotations
from src.scheduler.simulation import run_simulation
from src.scheduler.rules import build_active_rules
from src.plan_generator import generate_plans_for_all_buses
from src.plan_assigner import assign_plans, validate_assignments
from src.models import (
    Scenario,
    SchedulerResult,
    World,
)
from src.loader import load_all_scenarios, load_world, minutes_to_time_str

import sys
from pathlib import Path

import streamlit as st

# ensure src/ is importable when running from project root
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Minimal custom styling
# Clean, readable, professional — matches the operational/engineering tone
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
    /* tighten default streamlit padding */
    .block-container { padding-top: 2rem; padding-bottom: 2rem; }

    /* section headers */
    .section-header {
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: #888;
        margin-bottom: 0.5rem;
        margin-top: 1.5rem;
        padding-bottom: 0.4rem;
        border-bottom: 1px solid #e0e0e0;
    }

    /* stat cards */
    .stat-row {
        display: flex;
        gap: 1rem;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
    }
    .stat-card {
        background: #f8f9fa;
        border: 1px solid #e9ecef;
        border-radius: 6px;
        padding: 0.75rem 1.25rem;
        min-width: 140px;
    }
    .stat-label {
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #888;
        margin-bottom: 0.2rem;
    }
    .stat-value {
        font-size: 1.4rem;
        font-weight: 700;
        color: #1a1a2e;
    }
    .stat-unit {
        font-size: 0.75rem;
        color: #888;
        margin-left: 0.2rem;
    }

    /* wait time coloring in dataframes */
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Data loading — cached so it only runs once per session
# ---------------------------------------------------------------------------


@st.cache_data
def load_data() -> tuple[World, list[Scenario]]:
    """Load world and all scenarios. Cached across reruns."""
    world = load_world(Path(__file__).parent / "world.yaml")
    scenarios = load_all_scenarios(Path(__file__).parent / "scenarios")
    return world, scenarios


@st.cache_data
def run_scenario(scenario_id: str) -> SchedulerResult:
    """
    Run the full scheduling pipeline for a scenario.
    Cached by scenario_id so switching scenarios doesn't re-run previous ones.
    """
    world, scenarios = load_data()
    scenario = next(s for s in scenarios if s.scenario_id == scenario_id)

    valid_plans = generate_plans_for_all_buses(scenario.buses, world)
    assigned = assign_plans(scenario.buses, valid_plans, world)

    errors = validate_assignments(assigned, scenario.buses, world)
    if errors:
        raise ValueError(f"Plan validation failed: {errors}")

    weights = scenario.weights
    rules = build_active_rules(world, scenario)
    return run_simulation(scenario, world, assigned, weights, rules)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _operator_badge(operator: str) -> str:
    colors = {
        "kpn": "#1a6b3c",
        "freshbus": "#1a4a8a",
        "flixbus": "#6b1a6b",
    }
    color = colors.get(operator.lower(), "#555")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:3px;font-size:0.75rem;font-weight:600;">{operator.upper()}</span>'


def _wait_color(wait_min: int) -> str:
    """Return a background color based on wait severity."""
    if wait_min == 0:
        return "#e8f5e9"  # green — no wait
    elif wait_min <= 30:
        return "#fff8e1"  # yellow — moderate
    elif wait_min <= 60:
        return "#fff3e0"  # orange — significant
    else:
        return "#fce4ec"  # red — heavy


def _fmt_time(minutes: int) -> str:
    return minutes_to_time_str(minutes)


def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m:02d}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def render_scenario_input(scenario_id: str) -> None:
    """Section 1 — raw scenario input data."""
    world, scenarios = load_data()
    scenario = next(s for s in scenarios if s.scenario_id == scenario_id)

    st.markdown(
        '<div class="section-header">Scenario Input</div>', unsafe_allow_html=True
    )

    col1, col2 = st.columns([2, 1])

    with col1:
        # bus table
        rows = []
        for bus in scenario.buses:
            rows.append(
                {
                    "Bus ID": bus.id,
                    "Operator": bus.operator.upper(),
                    "Origin": bus.origin.capitalize(),
                    "Destination": bus.destination.capitalize(),
                    "Departure": bus.departure,
                }
            )

        import pandas as pd

        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)

    with col2:
        # weights card
        st.markdown("**Objective Weights**")
        w = scenario.weights
        st.metric("Individual", w["individual"])
        st.metric("Operator", w["operator"])
        st.metric("Overall", w["overall"])


def render_summary_stats(result: SchedulerResult) -> None:
    """Summary stat cards."""
    waits = [r.total_wait_min for r in result.bus_results]
    trips = [r.total_trip_min for r in result.bus_results]
    buses_no_wait = sum(1 for w in waits if w == 0)

    st.markdown(
        f"""
        <div class="stat-row">
            <div class="stat-card">
                <div class="stat-label">Buses Scheduled</div>
                <div class="stat-value">{len(result.bus_results)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Avg Wait</div>
                <div class="stat-value">{sum(waits) / len(waits):.0f}<span class="stat-unit">min</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Max Wait</div>
                <div class="stat-value">{max(waits)}<span class="stat-unit">min</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">No Wait</div>
                <div class="stat-value">{buses_no_wait}<span class="stat-unit">buses</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Avg Trip</div>
                <div class="stat-value">{sum(trips) / len(trips):.0f}<span class="stat-unit">min</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Max Trip</div>
                <div class="stat-value">{max(trips)}<span class="stat-unit">min</span></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_bus_timetable(result: SchedulerResult) -> None:
    """Section 2 — per-bus timetable."""
    import pandas as pd

    st.markdown(
        '<div class="section-header">Per-Bus Timetable</div>', unsafe_allow_html=True
    )

    rows = []
    for br in result.bus_results:
        # build charging stop detail string
        stop_details = []
        for ce in br.charge_events:
            wait_str = f" (+{ce.wait_min}m wait)" if ce.wait_min > 0 else ""
            stop_details.append(
                f"{ce.station} {_fmt_time(ce.charge_start_min)}→{_fmt_time(ce.charge_end_min)}{wait_str}"
            )

        rows.append(
            {
                "Bus": br.bus_id,
                "Operator": br.operator.upper(),
                "Route": f"{br.origin.capitalize()} → {br.destination.capitalize()}",
                "Departs": _fmt_time(br.departure_min),
                "Plan": " → ".join(br.charging_plan),
                "Charging Stops": " | ".join(stop_details) if stop_details else "—",
                "Wait (min)": br.total_wait_min,
                "Arrives": _fmt_time(br.arrival_min),
                "Trip Time": _fmt_duration(br.total_trip_min),
            }
        )

    df = pd.DataFrame(rows)

    # color wait column
    def color_wait(val: int) -> str:
        return f"background-color: {_wait_color(val)}"

    styled = df.style.map(color_wait, subset=["Wait (min)"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # legend
    st.markdown(
        """
        <div style="display:flex;gap:1rem;margin-top:0.5rem;font-size:0.75rem;color:#888;">
            <span style="background:#e8f5e9;padding:2px 8px;border-radius:3px;">No wait</span>
            <span style="background:#fff8e1;padding:2px 8px;border-radius:3px;">1–30 min</span>
            <span style="background:#fff3e0;padding:2px 8px;border-radius:3px;">31–60 min</span>
            <span style="background:#fce4ec;padding:2px 8px;border-radius:3px;">&gt;60 min</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_station_view(result: SchedulerResult) -> None:
    """Section 3 — per-station charging order."""
    import pandas as pd

    st.markdown(
        '<div class="section-header">Per-Station Charging Order</div>',
        unsafe_allow_html=True,
    )

    # map bus_id → operator from bus_results
    bus_operator = {br.bus_id: br.operator for br in result.bus_results}

    cols = st.columns(len(result.station_results))

    for col, sr in zip(cols, result.station_results, strict=True):
        with col:
            st.markdown(f"**Station {sr.station_id}**")

            rows = []
            seq = 1
            for slot in sr.slots:
                if len(sr.slots) > 1:
                    slot_label = f"Slot {slot.slot_index + 1}"
                else:
                    slot_label = None

                for ce in slot.events:
                    # find bus_id from result — match by charge_start_min and station
                    bus_id = _find_bus_for_event(
                        result, sr.station_id, ce.charge_start_min
                    )
                    operator = bus_operator.get(bus_id, "?")

                    row: dict[str, object] = {
                        "#": seq,
                        "Bus": bus_id,
                        "Op": operator.upper(),
                        "Start": _fmt_time(ce.charge_start_min),
                        "End": _fmt_time(ce.charge_end_min),
                        "Wait": f"{ce.wait_min}m" if ce.wait_min > 0 else "—",
                    }
                    if slot_label:
                        row["Slot"] = slot_label
                    rows.append(row)
                    seq += 1

            if rows:
                df = pd.DataFrame(rows)
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    height=min(400, 50 + len(rows) * 35),
                )
            else:
                st.caption("No buses charged here")


def _find_bus_for_event(
    result: SchedulerResult,
    station_id: str,
    charge_start_min: int,
) -> str:
    """Find bus_id that charged at station_id starting at charge_start_min."""
    for br in result.bus_results:
        for ce in br.charge_events:
            if ce.station == station_id and ce.charge_start_min == charge_start_min:
                return br.bus_id
    return "?"


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    # header
    st.title("⚡ Bus Charging Scheduler")
    st.caption(
        "Event-driven simulation with weighted fairness scheduling · "
        "Bengaluru → Kochi corridor"
    )

    # load data
    try:
        world, scenarios = load_data()
    except Exception as e:
        st.error(f"Failed to load world/scenarios: {e}")
        st.stop()

    # scenario dropdown
    scenario_options = {s.scenario_id: s for s in scenarios}
    display_names = {
        sid: f"{i + 1}. {s.description}"
        for i, (sid, s) in enumerate(scenario_options.items())
    }

    selected_id = st.selectbox(
        "Select Scenario",
        options=list(scenario_options.keys()),
        format_func=lambda sid: display_names[sid],
    )

    if not selected_id:
        st.stop()

    selected_scenario = scenario_options[selected_id]

    # scenario description + weights callout
    col_desc, col_weights = st.columns([3, 1])
    with col_desc:
        st.info(f"**{selected_scenario.description}**")
    with col_weights:
        w = selected_scenario.weights
        st.caption(
            f"Weights — individual: **{w['individual']}** · "
            f"operator: **{w['operator']}** · "
            f"overall: **{w['overall']}**"
        )

    # run simulation
    with st.spinner("Running scheduler..."):
        try:
            result = run_scenario(selected_id)
        except Exception as e:
            st.error(f"Scheduler error: {e}")
            st.stop()

    # summary stats
    render_summary_stats(result)

    # section 1 — scenario input
    render_scenario_input(selected_id)

    # section 2 — per-bus timetable
    render_bus_timetable(result)

    # section 3 — per-station view
    render_station_view(result)


if __name__ == "__main__":
    main()
