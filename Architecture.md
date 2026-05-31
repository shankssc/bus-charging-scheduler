# Architecture

## Scheduling Framework

### Approach: Event-Driven Simulation with Pluggable Rules

The scheduler is a **discrete event simulation** driven by a min-heap priority queue. It does not run in time ticks — it jumps directly to the next meaningful moment (bus arrives, bus finishes charging, charger cooldown ends) and processes only those moments.

This is the right fit for this problem because:

- **Hard constraints are structural, not optimisation targets.** Range limits and charger capacity are enforced before and during simulation — the range constraint is caught at plan generation time, charger exclusivity is maintained by slot management in the event loop. These don't need a solver.
- **Soft objectives are local decisions.** Every time a charger becomes free, we score the waiting buses and dispatch the best one. This is a priority queue decision, not a global optimisation — and it maps directly to how a real dispatch system would work in production.
- **The problem is real-time by nature.** EV charging networks make dispatch decisions as buses arrive, not as batch jobs. An event-driven simulation mirrors the actual operational model.
- **Extensibility is clean.** New event types, new rule objects, new scoring terms — none require touching existing code.

A constraint solver (e.g. OR-Tools CP-SAT) was considered. It would express hard constraints elegantly but introduces significant complexity: opaque infeasibility errors, a learning curve for live extension during the interview, and an architecture that doesn't match the real-time nature of the domain. The greedy simulation with pluggable rules achieves the same extensibility story with full transparency.

---

## Data Structure Design

### Two-file separation: `world.yaml` + `scenario_N.yaml`

The physical world (route, stations, operators, rules) never changes between scenarios. Scenario files describe only what differs: departure schedules, weights, and optional overrides.

This separation means:

- Adding a new scenario = one new YAML file, zero code changes
- Changing the physical world (new station, faster charger) = one edit to `world.yaml`, zero code changes
- The code never encodes assumptions about the specific route or operators

### Override chain

Every physical constant follows a three-level override chain:

```
bus-level override  →  stop-level override  →  world default
```

This applies to `battery_range_km`, `charge_duration_min`, `cooldown_min`, and `speed_kmph`. Absent means inherit. The chain is resolved in `src/loader.py` via `effective_*` helpers.

### Direction is implicit

A bus's travel direction is derived from `origin` and `destination` against the route stop list. There is no `direction` enum. This means adding a third terminus, a circular route, or a bidirectional branch requires no new enum values — the route graph handles it automatically.

---

## Anticipated Changes and How the Design Handles Them

This table covers every change that was anticipated during design. Each one is handled through data alone — no code changes required.

| Change                                    | How handled                                                                                                                                                                                                                         |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **New charging station**                  | Add stop + segment to `world.yaml`. Plan generator, simulation, and UI adapt automatically.                                                                                                                                         |
| **Multiple chargers at a station**        | Change `chargers: 1` to `chargers: N` on the stop in `world.yaml`. Simulation maintains N independent slots per station — no logic change.                                                                                          |
| **Faster or slower charger at a station** | Add `charge_duration_min: N` override to the stop in `world.yaml`. Resolved via override chain.                                                                                                                                     |
| **Cooldown period between buses**         | Add `cooldown_min: N` to the stop in `world.yaml`. Simulation schedules a `CHARGER_FREE` event after each charge instead of dispatching immediately.                                                                                |
| **Charger offline window**                | Add `offline_windows` list to stop in `world.yaml`. `DriverShiftRule` pattern extended to station availability.                                                                                                                     |
| **Global speed change**                   | Update `speed_kmph` in `world.yaml` defaults. All travel times re-derived automatically.                                                                                                                                            |
| **Per-segment speed**                     | Add `speed_kmph` override on the segment in `world.yaml`. Resolved before world default.                                                                                                                                            |
| **Temporary traffic event**               | Add to `segment_overrides` in scenario YAML. `_resolve_speed()` checks traffic event windows by time.                                                                                                                               |
| **New operator**                          | Add entry to `operators` list in `world.yaml`. No code references operator names.                                                                                                                                                   |
| **Priority buses**                        | Add `priority: true` to bus in scenario YAML. Enable `priority_bus_rule` in `world.yaml`. `PriorityBusRule` applies a large negative score modifier.                                                                                |
| **Different battery range per bus**       | Add `battery_range_km` override to the bus in scenario YAML. Resolved via override chain.                                                                                                                                           |
| **New scenario**                          | New file in `scenarios/`. App loads all `scenario_*.yaml` files on startup.                                                                                                                                                         |
| **Operator SLA (max wait)**               | Set `max_wait_min: N` on operator in `world.yaml`. Implement `OperatorSLARule` — pattern already shown in `rules.py`.                                                                                                               |
| **Time-of-day electricity costs**         | Enable `energy_cost_rule` in `world.yaml`. Add `cost_windows` to station data. `EnergyCostRule.score_modifier()` reads it. Stub already implemented.                                                                                |
| **Driver shift limits**                   | Enable `driver_shift_rule` in `world.yaml`. `DriverShiftRule.is_eligible()` blocks charging past shift end. Stub already implemented.                                                                                               |
| **Asymmetric scenario**                   | Just list fewer buses in one direction. Scenario 3 demonstrates this.                                                                                                                                                               |
| **Weight change**                         | Edit `weights` block in scenario YAML. One value, one place.                                                                                                                                                                        |
| **More routes sharing stations**          | Add a second route to `world.yaml`. Stations referenced by ID — shared stations appear in both route definitions. Plan generation would need a light extension to handle route-aware hop computation for buses on different routes. |

---

## Rule System

Rules are objects, not scattered conditionals.

```python
class Rule(ABC):
    def is_eligible(self, bus_id, time, bus_states, world) -> bool:
        # hard constraint — False blocks dispatch
        return True

    def score_modifier(self, bus_id, time, bus_states, world) -> float:
        # soft objective — adjusts priority score
        return 0.0
```

Hard rules (`is_eligible`) act as gates — a bus that fails any hard rule cannot be dispatched regardless of its score. Soft rules (`score_modifier`) shift the priority score up or down without blocking.

The simulation engine calls rules but never contains rule logic. Adding a new rule means adding a class and one conditional in `build_active_rules()` — the engine is untouched.

**Currently implemented rules:**

| Rule                               | Kind | Status                                         |
| ---------------------------------- | ---- | ---------------------------------------------- |
| `RangeRule`                        | Hard | Active (enforced at plan generation)           |
| `SingleChargerRule`                | Hard | Active (enforced by slot management)           |
| `RouteOrderRule`                   | Hard | Active (enforced at plan generation)           |
| Operator fairness (objective term) | Soft | Active (via `operator_term` in `objective.py`) |
| `PriorityBusRule`                  | Soft | Disabled (enable via world.yaml)               |
| `DriverShiftRule`                  | Hard | Disabled (enable via world.yaml)               |
| `EnergyCostRule`                   | Soft | Disabled (enable via world.yaml)               |

---

## Scoring Function

Called every time a charger becomes free and there are buses waiting. Lower score = higher priority.

```
score = w_individual * individual_term
      + w_operator   * operator_term
      + w_overall    * overall_term
      + sum(rule.score_modifier() for rule in active_rules)
```

**`individual_term`** = `-(total historical wait + current queue wait)`
Negative because more waiting = more deserving = lower score = higher priority.
Compensates buses that have been unlucky at earlier stations.

**`operator_term`** = `-(fleet average total wait across all buses from this operator)`
Negative for the same reason. When an operator's fleet is running behind, all buses from that operator get a boost. This is what makes Scenario 4 (operator weight = 2.0) produce visibly different queue orders.

**`overall_term`** = `-(estimated remaining trip time)`
Negative because more remaining trip = more urgent = higher priority.
Computed as remaining travel time through planned stops + charge time at remaining stops. Wait times excluded — not knowable at dispatch time. This is a documented approximation.

**Tie-breaking:** bus ID lexicographic order. Deterministic across runs.

**Changing a weight:** edit the `weights` block in the scenario YAML. No code changes. Setting a weight to `0.0` removes that term entirely — useful for testing weight sensitivity.

---

## Pre-Assignment vs Dynamic Planning

Plans are pre-assigned before simulation starts. For each bus, all valid charging plans are generated, scored by expected congestion at each station, and the least-congested plan is selected. Earlier buses get first pick (processing order is departure time).

**Why pre-assign:**

- Deterministic and debuggable — the plan is visible before simulation runs
- Simpler to own in production — operators can inspect plans upfront
- Sufficient at this scale — 20 buses, 8 valid plans each, small search space

**Dynamic planning alternative (deferred):**
Each bus could choose its next stop at departure time based on live queue lengths rather than upfront estimates. This reacts better to real-time disruptions (traffic events, charger failures) but makes global outcomes harder to reason about — local greedy decisions can produce worse total results. Switching to dynamic requires changing only `src/plan_assigner.py` — the simulation loop, scoring function, and Rule system are identical either way.

---

## Assumptions

- Travel speed is 60 km/h globally unless overridden. At this speed, distance in km equals travel time in minutes — a clean property of the default.
- All buses charge to full every time. Partial charging is not modelled (spec: "charging always to full").
- Charge time is 25 minutes regardless of remaining battery (spec: "always exactly 25 minutes"). Battery state is never tracked — only hop distances.
- Depot endpoints (Bengaluru, Kochi) fully charge buses before departure and are not scheduling resources.
- Congestion window for plan assignment is 30 minutes (≈ one charge cycle + buffer). Tunable via `window_min` parameter in `assign_plans()`.
- Naive arrival estimates for plan assignment ignore wait times (not yet known). This is intentional — approximation is sufficient for load distribution.
- Traffic events use departure time as the reference for speed resolution. A bus that enters a slow segment mid-event uses the reduced speed for the full segment — conservative approximation.
- Buses departing at identical times are processed in bus ID order for determinism.

---

## What I Would Do Next

- **Per-bus charge duration resolution** — the data model already supports bus-level overrides via `effective_charge_duration(bus, world, stop)` in `loader.py`. Wiring it through the simulation is a small, isolated change.
- **Dynamic plan selection** — as described above, the architecture supports it without engine changes.
- **Operator SLA enforcement** — `max_wait_min` is already in the data model. Implementing `OperatorSLARule` follows the exact pattern of `DriverShiftRule`.
- **Energy cost rule** — stub is implemented in `rules.py`. Needs `cost_windows` added to station data in `world.yaml` and the time-window check wired into `score_modifier()`.
- **Rolling-horizon simulation** for very large networks — process buses in time windows rather than all at once. Same event loop, different batching strategy.
