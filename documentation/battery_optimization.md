# Battery Optimization

The battery is scheduled by a single **linear program (LP)** solved over the whole price horizon
(the same PuLP/GLPK solver used for heat-pump scheduling). One global optimization decides what the
battery does in every 15-minute slot — grid-charge, discharge to cover household load, or hold
(reserve) — so the plan is optimal across the entire horizon rather than assembled from per-slot
rules of thumb.

> This replaces the previous approach (historical price percentiles + a price-difference arbitrage
> rule + a greedy SOC "cycle limiter" with charge/discharge buffers). Those heuristics could not see
> the whole day at once, so they would, for example, spend cheap stored energy on a cheap afternoon
> and arrive at the evening peak with an empty battery. The LP reserves energy for the dearest hours
> by construction.

## What the LP optimizes

For each slot `t` the solver chooses how much to grid-charge, store from solar surplus, and
discharge, to **minimize total energy cost over the horizon**:

```
minimize  Σ ( import_price[t]·grid_import[t] − export_price[t]·grid_export[t]
              + wear·battery_throughput[t] )  −  terminal_value·stored_energy_at_end
```

subject to the physics of the battery:

- **SOC dynamics** with round-trip efficiency (the loss is charged to the import side, so delivered
  energy is counted 1:1 — a cycle needs a price spread bigger than the losses to be worth it).
- **SOC stays within** `battery_min_soc_percent … battery_max_soc_percent`.
- **Charge/discharge rate** capped by `battery_charge_speed_kw`.
- **Discharge only covers household load** (the battery is never sold back to the grid), and **solar
  surplus can charge the battery** instead of being exported.
- **Terminal value** on energy left at the horizon end, so the plan doesn't myopically dump the
  battery in the last cheap slots. It defaults to `efficiency × 25th-percentile horizon price` —
  roughly what the energy cost to store, which keeps retained energy without inviting grid-charging
  purely to hoard.

The LP's per-slot decision is mapped back onto the existing inverter modes: a slot it grid-charges
becomes a `_charge` entry, a slot it discharges becomes a `_discharge` entry, and every other slot
is left in the inverter's hold state (which still passively absorbs solar surplus). Grid-export
blocking is unchanged and handled separately (see `price_based_solar_grid_export` /
`grid_export_block_threshold`).

## When it runs

The LP runs inside `recalculate_battery_limits`:

- **Once at the end of the daily optimization** (and at start-up), which also records the plan as
  the dashboard's "planned" reference.
- **Every 15 minutes**, re-solving from the **latest measured SOC** so the plan adapts as real SOC
  drifts from the prediction. Slots already in the past are preserved unchanged.

## Seasons: no switch needed

Seasonal behaviour is emergent, not coded:

- **Summer** (lots of solar, low load): the cheapest way to serve the evening is to store the midday
  surplus, so the LP charges the battery from solar during the day and discharges in the evening,
  grid-charging little or not at all.
- **Winter** (little solar): the LP arbitrages — charging from the grid in the cheapest overnight
  hours and discharging through the expensive evening peak.

There is no longer a whole-day "solar-only mode" flag; the per-slot optimization covers it.

## Configuration

The only battery tuning parameters are the two physical/economic ones:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `battery_round_trip_efficiency` | Fraction of energy surviving a charge/discharge cycle. Sets the price spread a cycle must beat to be profitable (e.g. 0.9 → ~11%). | 0.9 |
| `battery_cycle_cost_per_kwh` | Battery-wear hurdle (€/kWh) added to each kWh charged, to discourage cycling for tiny spreads. | 0.01 |

Per-device battery physicals live in the device config (`/data/options.json`):
`battery_capacity_kwh`, `battery_charge_speed_kw`, `battery_min_soc_percent`,
`battery_max_soc_percent`, and `battery_soc_entity` (the live SOC sensor the 15-minute re-solve
reads). Exported solar is valued at the day-ahead price, zeroed in slots where
`grid_export_block_threshold` blocks export.

> **Removed settings.** `battery_charge_percentile`, `battery_discharge_percentile`,
> `battery_price_history_days`, `battery_price_difference_threshold`,
> `battery_charge_time_percentage`, `battery_discharge_time_percentage`,
> `battery_charge_buffer_percent`, and `battery_discharge_buffer_percent` no longer do anything and
> can be deleted from `config.json`.

## Logging

Each solve logs a one-line summary, e.g.:

```
🔋 battery: LP plan — charge 5 slots (6.63 kWh grid @ 0.2313), discharge 24 slots (7.28 kWh @ 0.3542),
   stored 0.00 kWh solar, end SOC 10.0%, peak(≥0.40) covered 2.21 kWh, net +0.81 EUR
```

The dashboard's **Battery Plan** panel shows the same figures per device (grid-charged kWh and its
average price, discharged kWh and its average price, solar stored, net benefit, and projected end
SOC), and the schedule Gantt shows the planned vs. live charge/discharge slots alongside the
projected SOC curve.

## Troubleshooting

### Battery never charges or discharges
- On a genuinely flat-price day, a cycle can't clear the round-trip loss + wear hurdle, so the LP
  may legitimately do nothing — the per-slot summary in the log says so.
- Check `battery_capacity_kwh` and `battery_charge_speed_kw` are set for the device; without them
  the LP is skipped for that device (a warning is logged).
- Check the SOC sensor (`battery_soc_entity`) is readable — the 15-minute re-solve needs it.

### Want more or less cycling
- Lower `battery_cycle_cost_per_kwh` toward 0 to cycle for smaller spreads; raise it (e.g. 0.03–0.05)
  to only cycle for large spreads.
- `battery_round_trip_efficiency` toward 1.0 makes cycling easier to justify (less loss modelled);
  lower it to be stricter.
