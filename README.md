# Bus Charging Scheduler

Event-driven simulation for scheduling electric bus charging along the Bengaluru → Kochi corridor. Built with Python and Streamlit.

## Live App

> [Live Streamlit Community Cloud Deployment URL](https://bus-charging-scheduler-cuutpzjvi2k4p8pzywr8bx.streamlit.app/)

## Running Locally

**Requirements:** Python 3.12+

```bash
# clone the repo
git clone <your-repo-url>
cd bus-charging-scheduler

# create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# install dependencies
pip install -r requirements-dev.txt

# run the app
streamlit run app.py
```

The app opens at `http://localhost:8501`.

## Project Structure

```
app.py                  # Streamlit entry point
world.yaml              # Physical world — route, stations, operators, rules
requirements.txt        # Runtime dependencies (Streamlit Cloud reads this)
requirements-dev.txt    # Dev tools — ruff, mypy (local only)

scenarios/
  scenario_1.yaml       # Even spacing — baseline
  scenario_2.yaml       # Bunched start — heavy early contention
  scenario_3.yaml       # Asymmetric load — uneven directional traffic
  scenario_4.yaml       # Operator heavy — KPN dominance, operator weight = 2.0
  scenario_5.yaml       # Worst case convergence — maximum contention

src/
  models.py             # Typed dataclasses — World, Bus, Scenario, results
  loader.py             # YAML → model objects, time helpers
  plan_generator.py     # All valid charging plans per bus
  plan_assigner.py      # Congestion-aware plan selection (one plan per bus)
  scheduler/
    rules.py            # Rule base class + all rule implementations
    objective.py        # Weighted scoring function for queue dispatch
    simulation.py       # Event-driven simulation loop
```

## How to Change a Weight

Weights live in the scenario YAML file — one obvious place, no code changes.

Open `scenarios/scenario_4.yaml` (or any scenario file) and update the `weights` block:

```yaml
weights:
  individual: 1.0 # compensates buses that have waited longer
  operator: 2.0 # change this value ← one line, immediate effect
  overall: 1.0 # prioritizes buses with more remaining trip time
```

Reload the app. The scheduler re-runs with the new weights automatically.

**What each weight does:**

- `individual` — buses that have accumulated more total wait time get priority. Higher value = stronger compensation for delayed individual buses.
- `operator` — operators whose fleet is running behind on average get priority. Higher value = stronger fleet-level fairness. Setting to `0.0` removes operator-level consideration entirely.
- `overall` — buses with more remaining trip time get slight priority. Higher value = stronger bias toward clearing buses that have the most ground left to cover.

## How to Add a New Scenario

Create a new file in `scenarios/` — the app picks it up automatically on restart.

```yaml
# scenarios/scenario_6.yaml
scenario_id: scenario_6_my_scenario
description: "Description shown in the dropdown"
world_id: blr_kochi_v1

weights:
  individual: 1.0
  operator: 1.0
  overall: 1.0

buses:
  - id: bus-BK-01
    operator: kpn
    origin: bengaluru
    destination: kochi
    departure: "19:00"
  # add more buses...
```

No code changes required.

## How to Add a New Rule

**Step 1** — implement the rule class in `src/scheduler/rules.py`:

```python
class MinDwellRule(Rule):
    """Bus must wait at least N minutes at a station before charging starts."""

    def __init__(self, min_dwell_min: int = 5) -> None:
        self.min_dwell_min = min_dwell_min

    @property
    def rule_id(self) -> str:
        return "min_dwell_rule"

    def is_eligible(
        self,
        bus_id: str,
        time: float,
        bus_states: dict[str, BusState],
        world: World,
    ) -> bool:
        state = bus_states[bus_id]
        if state.wait_start is None:
            return True
        return (time - state.wait_start) >= self.min_dwell_min
```

**Step 2** — register it in `build_active_rules()` in the same file:

```python
if _rule_enabled(world, scenario, "min_dwell_rule"):
    rules.append(MinDwellRule(min_dwell_min=5))
```

**Step 3** — add the entry to `world.yaml`:

```yaml
rules:
  - id: min_dwell_rule
    kind: hard
    enabled: true
    description: "Bus must dwell at station for N minutes before charging"
```

That's it. Zero changes to the simulation engine.

## How to Add a New Station

Edit `world.yaml` only — no code changes:

```yaml
# add the stop
stops:
  - id: E
    kind: charger
    chargers: 1

# add the segment
segments:
  - from: D
    to: E
    distance_km: 60
  - from: E
    to: kochi
    distance_km: 40 # replaces old D→kochi segment
```

The plan generator, simulation, and UI all adapt automatically.

## How to Run Type Checking and Linting

```bash
# type checking
mypy src/ app.py

# linting
ruff check src/ app.py

# auto-fix and format
ruff check --fix src/ app.py && ruff format src/ app.py
```

## Deploying to Streamlit Community Cloud

1. Push the repo to GitHub (must be public)
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repo
4. Set main file to `app.py`
5. Deploy — Streamlit reads `requirements.txt` and installs dependencies automatically
