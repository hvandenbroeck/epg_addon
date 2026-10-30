# Heat-Pump Pre-Heating — Review Log

A running log of periodic assessments of the thermal pre-heating feature against real operation
data, so each review can build on the last with fresh data. See
[heat_pump_preheat.md](heat_pump_preheat.md) for the feature itself.

## How to run a review

1. From the Advanced SSH add-on shell: `python3 scripts/assess_preheat.py [hours_back]` (read-only;
   uses `$SUPERVISOR_TOKEN` + the add-on web UI on port 8099). It prints a Markdown block of
   comfort / setpoint-chatter / flow / prediction-accuracy / room-dynamics metrics.
2. Paste that block below under a new `### Review <date>` heading.
3. Add **Findings**, the **Changes made** (so the next review can attribute cause → effect), and
   update **Open items / next-review checklist**.
4. The feature stores what reviews need in TinyDB: `predictions.thermal_temp` (predicted room-temp
   trajectory), `predictions.thermal_temp_actual` (measured, appended every 15 min + daily HA
   backfill), `schedule.thermal_plan_summary` (economics), and the `wp` schedule ranges carry the
   commanded `setpoint`. Compare `thermal_temp` vs `thermal_temp_actual` for model accuracy.

## Baseline / context

- Feature went live **2026-10-29 ~22:14** (default `wp` device, `wp_preheat_enabled=true`).
- Config: own-setpoint via `z2quickvetotemp` `{setpoint}`; Balanced floors (19.5 asleep / 20.5 home
  06:30–08:30 & 17:00–23:00 + weekends / 19.5 weekday-away); cap 22.0 °C.
- Thermal model defaults (seeded, uncertain): `UA=0.024 kW/°C`, `C=1.5 kWh/°C`, `max_heat=5.0 kW`,
  COP 3.5 @ 7 °C, slope 0.08.
- House character (from 10-day fit): very high thermal mass, room temp extremely stable.

## Open items / next-review checklist

- [ ] **Model calibration (top priority, needs clean post-chatter data).** The room moved only
  ~0.6 °C/day while setpoints spanned 2.5 °C — control authority is far weaker/slower than the
  model assumes. Cannot refit `C`/`max_heat` reliably yet because (a) Review 1 data was chattering
  and (b) no reliable heat-input measurement. Next review: with chatter fixed, compare
  `thermal_temp` vs `thermal_temp_actual` (RMSE/bias now computed by the script) and adjust `C`
  (likely **raise** it / **lower** `max_heat` so predicted swings match the small measured swings).
- [ ] **Better heat-input signal.** `prenergysumhc` updates only ~daily — too coarse for per-hour
  load-shift or energy-balance UA refit. Consider logging flow temp × a nominal ΔT, or ask whether
  a finer HP power/energy sensor exists.
- [ ] **Quantify savings.** Once energy data is finer, compare actual heating cost vs a flat-setpoint
  baseline to confirm the feature actually saves money (not just shifts comfort).
- [ ] Confirm the anti-chatter fix held (expect setpoint value-changes to drop from ~70/day to a
  handful, and ebusd to log far fewer setpoint points).

---

### Review 2026-10-30 (~1 day after go-live)

```
- Data window: 2026-10-29T13:45Z → 2026-10-30T19:45Z (UTC); samples: room 27, setpoint 72, flow 69
- Comfort: room 20.56–21.19 °C (mean 20.84); floor breaches: 0 (comfort held)
- Setpoint: 72 writes / 25.9 h = 2.8 per h; 71 value-changes, 63 reversals (A→B→A = chatter);
  distinct values [20.0, 20.5, 21.0, 21.5, 22.0]
- Veto mode: last = 'veto' at Thu 22:14 (add-on in control)
- Outside: 5.4–15.3 °C (mean 9.4)
- Flow temp: 21.6–28.4 °C (mean 23.6) — continuously circulating
- Heating energy counter: 11332 → 11338 kWh (Δ6 kWh; only 2 points — too coarse)
- Room dynamics: observed dT/dt range −0.150…+0.150 °C/h (weak/slow control authority)
```

**Findings**

1. **Control works end-to-end.** Veto engaged at go-live; setpoints are written; comfort held all
   day (room 20.56–21.19, never below floor); the room did gently coast overnight
   (20.88 → 20.56 by 04:37) and warm through the day — the intended shape.
2. **Severe setpoint chatter (main defect).** 63 of 71 setpoint changes were A→B→A reversals,
   flipping 20.5 ↔ 21.0 every few minutes. Cause: the continuous optimum sits near a 0.5 °C
   rounding boundary (~20.7), so nearest-rounding flips both between adjacent slots and between
   15-min re-solves; the 5-min verifier + per-recalc re-apply amplify the write rate. Cosmetic +
   perturbs the HP's weather-comp + blunts the strategy.
3. **Weak control authority / model over-optimistic.** Room moved only ~0.6 °C across a day while
   setpoints spanned 19.5–22.0; a sustained 22 °C command (13:15–15:00) barely moved it. Predicted
   swings are larger than reality. Model `C`/`max_heat` need recalibration — deferred (data
   contaminated by chatter; no clean heat-input signal).

**Changes made (this review)**

- **Anti-chatter hysteresis** in `optimization/thermal_preheat.py`: sticky-staircase rounding with a
  0.375 °C deadband, seeded from the setpoint currently commanded (`previous_setpoint`, read from the
  live schedule in `optimizer.recalculate_thermal_setpoints`). Unit-tested: 12 boundary flips → 0,
  while genuine drifts still step cleanly. Expect setpoint changes to drop sharply.
- **Measured room temperature now logged + charted.** `recalculate_thermal_setpoints` appends the
  measured temp to `predictions.thermal_temp_actual` every 15 min and backfills from HA history at
  the daily run (new `HomeAssistantClient.get_history_series`, which passes `end_time`). The web
  Gantt's Heat-Pump row now plots **measured vs predicted** room temp alongside the setpoint command.
- **This review infrastructure**: `scripts/assess_preheat.py` + this log.

**Not changed (deferred, see checklist):** thermal `C` / `max_heat` calibration, savings
quantification — both need the cleaner data the above changes will now produce.
