window.PLAN_FIXTURES = window.PLAN_FIXTURES || {};
window.PLAN_FIXTURES["synthetic"] = {
 "label": "synthetic",
 "generated_at": "2026-10-11T21:10:00+01:00",
 "api_plan": {
  "plan": [
   {
    "entity_id": "sensor.ha_spark_charge_needed_kwh",
    "state": "12.20",
    "attributes": {
     "friendly_name": "ha-spark charge needed",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_target_soc",
    "state": "95",
    "attributes": {
     "friendly_name": "ha-spark target SoC",
     "unit_of_measurement": "%",
     "device_class": "battery"
    }
   },
   {
    "entity_id": "sensor.ha_spark_soc_now",
    "state": "34",
    "attributes": {
     "friendly_name": "ha-spark SoC now",
     "unit_of_measurement": "%",
     "device_class": "battery"
    }
   },
   {
    "entity_id": "sensor.ha_spark_forecast_load_kwh",
    "state": "22.06",
    "attributes": {
     "friendly_name": "ha-spark forecast load",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_solar_forecast_kwh",
    "state": "7.95",
    "attributes": {
     "friendly_name": "ha-spark solar forecast",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_deficit_kwh",
    "state": "13.44",
    "attributes": {
     "friendly_name": "ha-spark deficit",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_plan_status",
    "state": "slots",
    "attributes": {
     "friendly_name": "ha-spark plan status",
     "strategy": "deficit",
     "proactive_mode": "simulate",
     "soc_status": "ok",
     "soc_reason": "SoC 34% reported 40s ago"
    }
   },
   {
    "entity_id": "sensor.ha_spark_last_run",
    "state": "2026-10-14T15:41:24.721193+00:00",
    "attributes": {
     "friendly_name": "ha-spark last run",
     "device_class": "timestamp"
    }
   },
   {
    "entity_id": "sensor.ha_spark_planned_cost",
    "state": "-4.03",
    "attributes": {
     "friendly_name": "ha-spark planned cost",
     "unit_of_measurement": "GBP",
     "device_class": "monetary"
    }
   },
   {
    "entity_id": "sensor.ha_spark_baseline_cost",
    "state": "3.60",
    "attributes": {
     "friendly_name": "ha-spark baseline cost (no battery)",
     "unit_of_measurement": "GBP",
     "device_class": "monetary"
    }
   }
  ],
  "generated_at": "2026-10-11T21:10:00+01:00"
 },
 "explain": [
  "Charge plan:",
  "  SoC now            34%",
  "  Solar tomorrow     9.35 kWh (haircut -> 7.95)",
  "  Home load forecast 22.06 kWh  (slots)",
  "  Peak-slot load     13.44 kWh (after solar)",
  "  Slot import price  \u00a30.070\u2013\u00a30.277/kWh  (15/48 slots cheap)",
  "  Reservations:",
  "    axle-export-event  12.82 kWh  \u2014 Reserving 12.82 kWh for forecast house load before and during paid Axle export from 16:30 to 19:00, while protecting house load.",
  "    reach-next-cheap-slot  3.18 kWh  \u2014 Reserving 3.18 kWh after the Axle event through the planning horizon. The other 2.87 kWh funds the paid export and is bought back.",
  "  Paid export       16:30-19:00  1.70 kW planned (DNO limit 3.68 kW; 5 complete slots)",
  "    Funded 2.87 kWh from the post-event reserve: \u00a30.95 event vs \u00a30.31 buy-back.",
  "  Usable now         3.80 kWh  (-0.90 by window start -> 2.90)",
  "  Energy deficit     13.44 kWh  (+10% buffer -> 14.78)",
  "  Required charge    12.20 kWh  (funds axle-export-event, reach-next-cheap-slot)  (buy 13.56 @ 90% eff)",
  "  Charge to          95%  over the 6.0 h window (23:30-05:30)",
  "  EV                 not charging",
  "  EV dispatch energy 6.50 kWh planned by Octopus",
  "  Projected cost     \u00a3-4.03  (vs \u00a33.60 without battery \u2014 saving \u00a37.62)  (incl. export -\u00a34.96)"
 ],
 "timeline": {
  "horizon_start": "2026-10-11T23:30:00+01:00",
  "slot_minutes": 30,
  "now": "2026-10-11T21:10:00+01:00",
  "soc_now": 34.0,
  "target_soc": 95.0,
  "min_soc": 15.0,
  "capacity_kwh": 20.0,
  "proactive_mode": "simulate",
  "strategy": "deficit",
  "costs": {
   "planned": -4.026347818169935,
   "baseline": 3.59523876,
   "export_revenue": 4.963032500000001
  },
  "slots": [
   {
    "start": "2026-10-11T23:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 109,
    "carbon_index": "low",
    "soc_pct": 29.5,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T00:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "soc_pct": 34.6,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T00:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 87,
    "carbon_index": "low",
    "soc_pct": 39.7,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T01:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 78,
    "carbon_index": "low",
    "soc_pct": 44.8,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T01:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 63,
    "carbon_index": "low",
    "soc_pct": 49.8,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T02:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 53,
    "carbon_index": "low",
    "soc_pct": 54.9,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T02:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 55,
    "carbon_index": "low",
    "soc_pct": 60.0,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T03:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 67,
    "carbon_index": "low",
    "soc_pct": 65.1,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T03:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 81,
    "carbon_index": "low",
    "soc_pct": 70.2,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T04:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 89,
    "carbon_index": "low",
    "soc_pct": 75.2,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T04:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "soc_pct": 80.3,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T05:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "soc_pct": 85.4,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T05:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 67,
    "carbon_index": "low",
    "soc_pct": 90.5,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T06:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.35,
    "solar_kwh": 0.0,
    "carbon_gco2": 73,
    "carbon_index": "low",
    "soc_pct": 89.4,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T06:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.35,
    "solar_kwh": 0.0,
    "carbon_gco2": 92,
    "carbon_index": "low",
    "soc_pct": 87.7,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T07:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.0,
    "carbon_gco2": 119,
    "carbon_index": "low",
    "soc_pct": 85.9,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T07:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.016,
    "carbon_gco2": 138,
    "carbon_index": "moderate",
    "soc_pct": 82.7,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T08:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.084,
    "carbon_gco2": 141,
    "carbon_index": "moderate",
    "soc_pct": 79.5,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T08:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.177,
    "carbon_gco2": 126,
    "carbon_index": "moderate",
    "soc_pct": 76.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T09:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.283,
    "carbon_gco2": 101,
    "carbon_index": "low",
    "soc_pct": 74.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T09:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.394,
    "carbon_gco2": 80,
    "carbon_index": "low",
    "soc_pct": 73.4,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T10:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.502,
    "carbon_gco2": 71,
    "carbon_index": "low",
    "soc_pct": 73.2,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T10:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.601,
    "carbon_gco2": 72,
    "carbon_index": "low",
    "soc_pct": 73.4,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T11:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.684,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "soc_pct": 74.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T11:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.747,
    "carbon_gco2": 68,
    "carbon_index": "low",
    "soc_pct": 75.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T12:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.787,
    "carbon_gco2": 52,
    "carbon_index": "low",
    "soc_pct": 76.3,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T12:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.8,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 77.8,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T13:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.787,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 79.3,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T13:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.747,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 80.7,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T14:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.684,
    "carbon_gco2": 36,
    "carbon_index": "very low",
    "soc_pct": 82.0,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T14:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.601,
    "carbon_gco2": 58,
    "carbon_index": "low",
    "soc_pct": 83.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T15:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.502,
    "carbon_gco2": 77,
    "carbon_index": "low",
    "soc_pct": 83.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T15:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.394,
    "carbon_gco2": 88,
    "carbon_index": "low",
    "soc_pct": 83.9,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T16:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.55,
    "solar_kwh": 0.283,
    "carbon_gco2": 95,
    "carbon_index": "low",
    "soc_pct": 83.7,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T16:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.55,
    "solar_kwh": 0.177,
    "carbon_gco2": 106,
    "carbon_index": "low",
    "soc_pct": 82.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T17:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.084,
    "carbon_gco2": 129,
    "carbon_index": "moderate",
    "soc_pct": 73.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T17:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.016,
    "carbon_gco2": 159,
    "carbon_index": "moderate",
    "soc_pct": 64.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T18:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 183,
    "carbon_index": "moderate",
    "soc_pct": 55.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T18:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 187,
    "carbon_index": "moderate",
    "soc_pct": 46.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T19:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 166,
    "carbon_index": "moderate",
    "soc_pct": 37.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T19:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 129,
    "carbon_index": "moderate",
    "soc_pct": 32.4,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T20:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 91,
    "carbon_index": "low",
    "soc_pct": 27.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T20:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 66,
    "carbon_index": "low",
    "soc_pct": 24.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T21:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 60,
    "carbon_index": "low",
    "soc_pct": 21.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T21:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 69,
    "carbon_index": "low",
    "soc_pct": 18.6,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T22:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "soc_pct": 17.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T22:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 88,
    "carbon_index": "low",
    "soc_pct": 17.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T23:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "soc_pct": 17.0,
    "soc_kind": "path",
    "actions": []
   }
  ],
  "carbon_source": "synthetic (prototype)",
  "history": [
   {
    "start": "2026-10-11T07:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 127,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T07:30:00+01:00",
    "solar_forecast_kwh": 0.016,
    "solar_actual_kwh": 0.018,
    "carbon_gco2": 150,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T08:00:00+01:00",
    "solar_forecast_kwh": 0.084,
    "solar_actual_kwh": 0.094,
    "carbon_gco2": 165,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T08:30:00+01:00",
    "solar_forecast_kwh": 0.177,
    "solar_actual_kwh": 0.198,
    "carbon_gco2": 163,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T09:00:00+01:00",
    "solar_forecast_kwh": 0.283,
    "solar_actual_kwh": 0.317,
    "carbon_gco2": 144,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T09:30:00+01:00",
    "solar_forecast_kwh": 0.394,
    "solar_actual_kwh": 0.441,
    "carbon_gco2": 116,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T10:00:00+01:00",
    "solar_forecast_kwh": 0.502,
    "solar_actual_kwh": 0.562,
    "carbon_gco2": 92,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T10:30:00+01:00",
    "solar_forecast_kwh": 0.601,
    "solar_actual_kwh": 0.673,
    "carbon_gco2": 81,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T11:00:00+01:00",
    "solar_forecast_kwh": 0.684,
    "solar_actual_kwh": 0.766,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T11:30:00+01:00",
    "solar_forecast_kwh": 0.747,
    "solar_actual_kwh": 0.837,
    "carbon_gco2": 87,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T12:00:00+01:00",
    "solar_forecast_kwh": 0.787,
    "solar_actual_kwh": 0.881,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T12:30:00+01:00",
    "solar_forecast_kwh": 0.8,
    "solar_actual_kwh": 0.896,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T13:00:00+01:00",
    "solar_forecast_kwh": 0.787,
    "solar_actual_kwh": 0.433,
    "carbon_gco2": 58,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T13:30:00+01:00",
    "solar_forecast_kwh": 0.747,
    "solar_actual_kwh": 0.411,
    "carbon_gco2": 47,
    "carbon_index": "very low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T14:00:00+01:00",
    "solar_forecast_kwh": 0.684,
    "solar_actual_kwh": 0.376,
    "carbon_gco2": 49,
    "carbon_index": "very low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T14:30:00+01:00",
    "solar_forecast_kwh": 0.601,
    "solar_actual_kwh": 0.331,
    "carbon_gco2": 66,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T15:00:00+01:00",
    "solar_forecast_kwh": 0.502,
    "solar_actual_kwh": 0.276,
    "carbon_gco2": 91,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T15:30:00+01:00",
    "solar_forecast_kwh": 0.394,
    "solar_actual_kwh": 0.217,
    "carbon_gco2": 116,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T16:00:00+01:00",
    "solar_forecast_kwh": 0.283,
    "solar_actual_kwh": 0.156,
    "carbon_gco2": 134,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T16:30:00+01:00",
    "solar_forecast_kwh": 0.177,
    "solar_actual_kwh": 0.097,
    "carbon_gco2": 148,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T17:00:00+01:00",
    "solar_forecast_kwh": 0.084,
    "solar_actual_kwh": 0.046,
    "carbon_gco2": 162,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T17:30:00+01:00",
    "solar_forecast_kwh": 0.016,
    "solar_actual_kwh": 0.009,
    "carbon_gco2": 178,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T18:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 192,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T18:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 197,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T19:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 188,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T19:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 165,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T20:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 134,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T20:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 104,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T21:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 84,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T21:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 80,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T22:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 90,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T22:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 105,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T23:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 113,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   }
  ],
  "lines": [
   {
    "kind": "reserve",
    "start": "2026-10-11T21:10:00+01:00",
    "end": "2026-10-12T19:00:00+01:00",
    "kwh": 12.82,
    "text": "Reserving 12.82 kWh for forecast house load before and during paid Axle export from 16:30 to 19:00, while protecting house load.",
    "source": "planner"
   },
   {
    "kind": "reserve",
    "start": "2026-10-11T21:10:00+01:00",
    "end": "2026-10-12T23:30:00+01:00",
    "kwh": 3.18,
    "text": "Reserving 3.18 kWh after the Axle event through the planning horizon. The other 2.87 kWh funds the paid export and is bought back.",
    "source": "planner"
   },
   {
    "kind": "charge",
    "start": "2026-10-11T23:30:00+01:00",
    "end": "2026-10-12T05:30:00+01:00",
    "text": "Charge 34% \u2192 95% (12.2 kWh) in the cheap window.",
    "source": "planner-numbers"
   },
   {
    "kind": "hold",
    "start": "2026-10-12T13:00:00+01:00",
    "end": "2026-10-12T14:30:00+01:00",
    "text": "Hold the battery: a smart-charge dispatch makes grid power cheap, so the house runs from the grid.",
    "source": "prototype-wording"
   },
   {
    "kind": "export",
    "start": "2026-10-12T16:30:00+01:00",
    "end": "2026-10-12T19:00:00+01:00",
    "text": "Export 1.7 kW for the flexibility event (Funded 2.87 kWh from the post-event reserve: \u00a30.95 event vs \u00a30.31 buy-back.)",
    "source": "planner-numbers"
   },
   {
    "kind": "v2l",
    "start": "2026-10-12T21:30:00+01:00",
    "end": "2026-10-12T23:00:00+01:00",
    "kwh": 1.08,
    "text": "Refill the house battery with 1.08 kWh from V2L for the reach-next-cheap-slot reservation.",
    "source": "planner",
    "slots": 3
   }
  ]
 }
};
window.PLAN_FIXTURES["tight"] = {
 "label": "tight",
 "generated_at": "2026-10-11T21:10:00+01:00",
 "api_plan": {
  "plan": [
   {
    "entity_id": "sensor.ha_spark_charge_needed_kwh",
    "state": "7.32",
    "attributes": {
     "friendly_name": "ha-spark charge needed",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_target_soc",
    "state": "95",
    "attributes": {
     "friendly_name": "ha-spark target SoC",
     "unit_of_measurement": "%",
     "device_class": "battery"
    }
   },
   {
    "entity_id": "sensor.ha_spark_soc_now",
    "state": "34",
    "attributes": {
     "friendly_name": "ha-spark SoC now",
     "unit_of_measurement": "%",
     "device_class": "battery"
    }
   },
   {
    "entity_id": "sensor.ha_spark_forecast_load_kwh",
    "state": "22.06",
    "attributes": {
     "friendly_name": "ha-spark forecast load",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_solar_forecast_kwh",
    "state": "7.95",
    "attributes": {
     "friendly_name": "ha-spark solar forecast",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_deficit_kwh",
    "state": "13.44",
    "attributes": {
     "friendly_name": "ha-spark deficit",
     "unit_of_measurement": "kWh",
     "device_class": "energy"
    }
   },
   {
    "entity_id": "sensor.ha_spark_plan_status",
    "state": "slots",
    "attributes": {
     "friendly_name": "ha-spark plan status",
     "strategy": "deficit",
     "proactive_mode": "simulate",
     "soc_status": "ok",
     "soc_reason": "SoC 34% reported 40s ago",
     "export_soc_needed_pct": 140,
     "export_soc_needed_at": "2026-10-12T16:30:00+01:00"
    }
   },
   {
    "entity_id": "sensor.ha_spark_last_run",
    "state": "2026-10-14T15:41:24.724318+00:00",
    "attributes": {
     "friendly_name": "ha-spark last run",
     "device_class": "timestamp"
    }
   },
   {
    "entity_id": "sensor.ha_spark_planned_cost",
    "state": "0.65",
    "attributes": {
     "friendly_name": "ha-spark planned cost",
     "unit_of_measurement": "GBP",
     "device_class": "monetary"
    }
   },
   {
    "entity_id": "sensor.ha_spark_baseline_cost",
    "state": "3.60",
    "attributes": {
     "friendly_name": "ha-spark baseline cost (no battery)",
     "unit_of_measurement": "GBP",
     "device_class": "monetary"
    }
   }
  ],
  "generated_at": "2026-10-11T21:10:00+01:00"
 },
 "explain": [
  "Charge plan:",
  "  SoC now            34%",
  "  Solar tomorrow     9.35 kWh (haircut -> 7.95)",
  "  Home load forecast 22.06 kWh  (slots)",
  "  Peak-slot load     13.44 kWh (after solar)",
  "  Slot import price  \u00a30.070\u2013\u00a30.277/kWh  (15/48 slots cheap)",
  "  Reservations:",
  "    axle-export-event  8.79 kWh  \u2014 Reserving 8.79 kWh for forecast house load before and during paid Axle export from 18:30 to 19:00, while protecting house load.",
  "    reach-next-cheap-slot  5.20 kWh  \u2014 Reserving 5.20 kWh after the Axle event through the planning horizon. The other 0.85 kWh funds the paid export and is bought back.",
  "  Paid export       18:30-19:00  1.70 kW planned (DNO limit 3.68 kW; 1 complete slots)",
  "    Funded 0.85 kWh from the post-event reserve: \u00a30.95 event vs \u00a30.31 buy-back.",
  "  Skipped paid export slots:",
  "    Event needs 140% SoC at 16:30 (now 34%) \u2014 more than a full battery can fund",
  "    16:30  \u2014 Skipped paid slot: a later slot is unavailable, so one timed window cannot cover it.",
  "    17:00  \u2014 Skipped paid slot: a later slot is unavailable, so one timed window cannot cover it.",
  "    17:30  \u2014 Skipped paid slot: a later slot is unavailable, so one timed window cannot cover it.",
  "    18:00  \u2014 Skipped paid slot: funding it would consume reserved house or post-event energy.",
  "  Usable now         2.28 kWh  (-0.90 by window start -> 1.38)",
  "  Energy deficit     13.44 kWh  (+10% buffer -> 14.78)",
  "  Required charge    7.32 kWh  (funds axle-export-event, reach-next-cheap-slot)  (buy 8.13 @ 90% eff)",
  "  Charge to          95%  over the 6.0 h window (23:30-05:30)",
  "  EV                 not charging",
  "  EV dispatch energy 6.50 kWh planned by Octopus",
  "  Projected cost     \u00a30.65  (vs \u00a33.60 without battery \u2014 saving \u00a32.94)  (incl. export -\u00a31.13)"
 ],
 "timeline": {
  "horizon_start": "2026-10-11T23:30:00+01:00",
  "slot_minutes": 30,
  "now": "2026-10-11T21:10:00+01:00",
  "soc_now": 34.0,
  "target_soc": 95.0,
  "min_soc": 15.0,
  "capacity_kwh": 12.0,
  "proactive_mode": "simulate",
  "strategy": "deficit",
  "costs": {
   "planned": 0.6514974693333335,
   "baseline": 3.59523876,
   "export_revenue": 1.1293549999999999
  },
  "slots": [
   {
    "start": "2026-10-11T23:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 109,
    "carbon_index": "low",
    "soc_pct": 26.5,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T00:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "soc_pct": 31.6,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T00:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 87,
    "carbon_index": "low",
    "soc_pct": 36.7,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T01:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 78,
    "carbon_index": "low",
    "soc_pct": 41.8,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T01:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 63,
    "carbon_index": "low",
    "soc_pct": 46.8,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T02:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 53,
    "carbon_index": "low",
    "soc_pct": 51.9,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T02:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 55,
    "carbon_index": "low",
    "soc_pct": 57.0,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T03:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 67,
    "carbon_index": "low",
    "soc_pct": 62.1,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T03:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 81,
    "carbon_index": "low",
    "soc_pct": 67.2,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T04:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 89,
    "carbon_index": "low",
    "soc_pct": 72.2,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T04:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "soc_pct": 77.3,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T05:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "soc_pct": 82.4,
    "soc_kind": "ramp",
    "actions": [
     "charge"
    ]
   },
   {
    "start": "2026-10-12T05:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.22,
    "solar_kwh": 0.0,
    "carbon_gco2": 67,
    "carbon_index": "low",
    "soc_pct": 87.5,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T06:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.35,
    "solar_kwh": 0.0,
    "carbon_gco2": 73,
    "carbon_index": "low",
    "soc_pct": 85.7,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T06:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.35,
    "solar_kwh": 0.0,
    "carbon_gco2": 92,
    "carbon_index": "low",
    "soc_pct": 82.8,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T07:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.0,
    "carbon_gco2": 119,
    "carbon_index": "low",
    "soc_pct": 79.8,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T07:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.016,
    "carbon_gco2": 138,
    "carbon_index": "moderate",
    "soc_pct": 74.4,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T08:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.084,
    "carbon_gco2": 141,
    "carbon_index": "moderate",
    "soc_pct": 69.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T08:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.65,
    "solar_kwh": 0.177,
    "carbon_gco2": 126,
    "carbon_index": "moderate",
    "soc_pct": 64.3,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T09:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.283,
    "carbon_gco2": 101,
    "carbon_index": "low",
    "soc_pct": 60.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T09:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.394,
    "carbon_gco2": 80,
    "carbon_index": "low",
    "soc_pct": 59.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T10:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.502,
    "carbon_gco2": 71,
    "carbon_index": "low",
    "soc_pct": 58.6,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T10:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.601,
    "carbon_gco2": 72,
    "carbon_index": "low",
    "soc_pct": 59.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T11:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.684,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "soc_pct": 60.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T11:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.747,
    "carbon_gco2": 68,
    "carbon_index": "low",
    "soc_pct": 61.7,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T12:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.787,
    "carbon_gco2": 52,
    "carbon_index": "low",
    "soc_pct": 63.9,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T12:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.8,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 66.3,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T13:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.787,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 68.8,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T13:30:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.747,
    "carbon_gco2": 35,
    "carbon_index": "very low",
    "soc_pct": 71.2,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T14:00:00+01:00",
    "import_price": 0.07,
    "export_price": 0.15,
    "cheap_frac": 1.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.684,
    "carbon_gco2": 36,
    "carbon_index": "very low",
    "soc_pct": 73.3,
    "soc_kind": "path",
    "actions": [
     "hold"
    ]
   },
   {
    "start": "2026-10-12T14:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.601,
    "carbon_gco2": 58,
    "carbon_index": "low",
    "soc_pct": 75.0,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T15:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.502,
    "carbon_gco2": 77,
    "carbon_index": "low",
    "soc_pct": 76.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T15:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.38,
    "solar_kwh": 0.394,
    "carbon_gco2": 88,
    "carbon_index": "low",
    "soc_pct": 76.5,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T16:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.55,
    "solar_kwh": 0.283,
    "carbon_gco2": 95,
    "carbon_index": "low",
    "soc_pct": 76.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T16:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.55,
    "solar_kwh": 0.177,
    "carbon_gco2": 106,
    "carbon_index": "low",
    "soc_pct": 73.5,
    "soc_kind": "path",
    "actions": [
     "skip"
    ]
   },
   {
    "start": "2026-10-12T17:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.084,
    "carbon_gco2": 129,
    "carbon_index": "moderate",
    "soc_pct": 70.2,
    "soc_kind": "path",
    "actions": [
     "skip"
    ]
   },
   {
    "start": "2026-10-12T17:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.016,
    "carbon_gco2": 159,
    "carbon_index": "moderate",
    "soc_pct": 62.9,
    "soc_kind": "path",
    "actions": [
     "skip"
    ]
   },
   {
    "start": "2026-10-12T18:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 183,
    "carbon_index": "moderate",
    "soc_pct": 55.1,
    "soc_kind": "path",
    "actions": [
     "skip"
    ]
   },
   {
    "start": "2026-10-12T18:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.95,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 187,
    "carbon_index": "moderate",
    "soc_pct": 47.1,
    "soc_kind": "path",
    "actions": [
     "export"
    ]
   },
   {
    "start": "2026-10-12T19:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 166,
    "carbon_index": "moderate",
    "soc_pct": 32.1,
    "soc_kind": "path",
    "actions": []
   },
   {
    "start": "2026-10-12T19:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.95,
    "solar_kwh": 0.0,
    "carbon_gco2": 129,
    "carbon_index": "moderate",
    "soc_pct": 24.2,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T20:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 91,
    "carbon_index": "low",
    "soc_pct": 20.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T20:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 66,
    "carbon_index": "low",
    "soc_pct": 20.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T21:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 60,
    "carbon_index": "low",
    "soc_pct": 20.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T21:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.6,
    "solar_kwh": 0.0,
    "carbon_gco2": 69,
    "carbon_index": "low",
    "soc_pct": 20.0,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T22:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "soc_pct": 18.3,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T22:30:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 88,
    "carbon_index": "low",
    "soc_pct": 18.3,
    "soc_kind": "path",
    "actions": [
     "v2l"
    ]
   },
   {
    "start": "2026-10-12T23:00:00+01:00",
    "import_price": 0.2768,
    "export_price": 0.15,
    "cheap_frac": 0.0,
    "load_kwh": 0.4,
    "solar_kwh": 0.0,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "soc_pct": 18.3,
    "soc_kind": "path",
    "actions": []
   }
  ],
  "carbon_source": "synthetic (prototype)",
  "history": [
   {
    "start": "2026-10-11T07:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 127,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T07:30:00+01:00",
    "solar_forecast_kwh": 0.016,
    "solar_actual_kwh": 0.018,
    "carbon_gco2": 150,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T08:00:00+01:00",
    "solar_forecast_kwh": 0.084,
    "solar_actual_kwh": 0.094,
    "carbon_gco2": 165,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T08:30:00+01:00",
    "solar_forecast_kwh": 0.177,
    "solar_actual_kwh": 0.198,
    "carbon_gco2": 163,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T09:00:00+01:00",
    "solar_forecast_kwh": 0.283,
    "solar_actual_kwh": 0.317,
    "carbon_gco2": 144,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T09:30:00+01:00",
    "solar_forecast_kwh": 0.394,
    "solar_actual_kwh": 0.441,
    "carbon_gco2": 116,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T10:00:00+01:00",
    "solar_forecast_kwh": 0.502,
    "solar_actual_kwh": 0.562,
    "carbon_gco2": 92,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T10:30:00+01:00",
    "solar_forecast_kwh": 0.601,
    "solar_actual_kwh": 0.673,
    "carbon_gco2": 81,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T11:00:00+01:00",
    "solar_forecast_kwh": 0.684,
    "solar_actual_kwh": 0.766,
    "carbon_gco2": 82,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T11:30:00+01:00",
    "solar_forecast_kwh": 0.747,
    "solar_actual_kwh": 0.837,
    "carbon_gco2": 87,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T12:00:00+01:00",
    "solar_forecast_kwh": 0.787,
    "solar_actual_kwh": 0.881,
    "carbon_gco2": 85,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T12:30:00+01:00",
    "solar_forecast_kwh": 0.8,
    "solar_actual_kwh": 0.896,
    "carbon_gco2": 74,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T13:00:00+01:00",
    "solar_forecast_kwh": 0.787,
    "solar_actual_kwh": 0.433,
    "carbon_gco2": 58,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T13:30:00+01:00",
    "solar_forecast_kwh": 0.747,
    "solar_actual_kwh": 0.411,
    "carbon_gco2": 47,
    "carbon_index": "very low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T14:00:00+01:00",
    "solar_forecast_kwh": 0.684,
    "solar_actual_kwh": 0.376,
    "carbon_gco2": 49,
    "carbon_index": "very low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T14:30:00+01:00",
    "solar_forecast_kwh": 0.601,
    "solar_actual_kwh": 0.331,
    "carbon_gco2": 66,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T15:00:00+01:00",
    "solar_forecast_kwh": 0.502,
    "solar_actual_kwh": 0.276,
    "carbon_gco2": 91,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T15:30:00+01:00",
    "solar_forecast_kwh": 0.394,
    "solar_actual_kwh": 0.217,
    "carbon_gco2": 116,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T16:00:00+01:00",
    "solar_forecast_kwh": 0.283,
    "solar_actual_kwh": 0.156,
    "carbon_gco2": 134,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T16:30:00+01:00",
    "solar_forecast_kwh": 0.177,
    "solar_actual_kwh": 0.097,
    "carbon_gco2": 148,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T17:00:00+01:00",
    "solar_forecast_kwh": 0.084,
    "solar_actual_kwh": 0.046,
    "carbon_gco2": 162,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T17:30:00+01:00",
    "solar_forecast_kwh": 0.016,
    "solar_actual_kwh": 0.009,
    "carbon_gco2": 178,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T18:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 192,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T18:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 197,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T19:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 188,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T19:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 165,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T20:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 134,
    "carbon_index": "moderate",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T20:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": 0.0,
    "carbon_gco2": 104,
    "carbon_index": "low",
    "carbon_kind": "actual"
   },
   {
    "start": "2026-10-11T21:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 84,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T21:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 80,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T22:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 90,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T22:30:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 105,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   },
   {
    "start": "2026-10-11T23:00:00+01:00",
    "solar_forecast_kwh": 0.0,
    "solar_actual_kwh": null,
    "carbon_gco2": 113,
    "carbon_index": "low",
    "carbon_kind": "forecast"
   }
  ],
  "lines": [
   {
    "kind": "reserve",
    "start": "2026-10-11T21:10:00+01:00",
    "end": "2026-10-12T19:00:00+01:00",
    "kwh": 8.79,
    "text": "Reserving 8.79 kWh for forecast house load before and during paid Axle export from 18:30 to 19:00, while protecting house load.",
    "source": "planner"
   },
   {
    "kind": "reserve",
    "start": "2026-10-11T21:10:00+01:00",
    "end": "2026-10-12T23:30:00+01:00",
    "kwh": 5.2,
    "text": "Reserving 5.20 kWh after the Axle event through the planning horizon. The other 0.85 kWh funds the paid export and is bought back.",
    "source": "planner"
   },
   {
    "kind": "charge",
    "start": "2026-10-11T23:30:00+01:00",
    "end": "2026-10-12T05:30:00+01:00",
    "text": "Charge 34% \u2192 95% (7.3 kWh) in the cheap window.",
    "source": "planner-numbers"
   },
   {
    "kind": "hold",
    "start": "2026-10-12T13:00:00+01:00",
    "end": "2026-10-12T14:30:00+01:00",
    "text": "Hold the battery: a smart-charge dispatch makes grid power cheap, so the house runs from the grid.",
    "source": "prototype-wording"
   },
   {
    "kind": "skip",
    "start": "2026-10-12T16:30:00+01:00",
    "end": "2026-10-12T18:00:00+01:00",
    "text": "Skipped paid slot: a later slot is unavailable, so one timed window cannot cover it.",
    "source": "planner",
    "slots": 3
   },
   {
    "kind": "skip",
    "start": "2026-10-12T18:00:00+01:00",
    "end": "2026-10-12T18:30:00+01:00",
    "text": "Skipped paid slot: funding it would consume reserved house or post-event energy.",
    "source": "planner"
   },
   {
    "kind": "export",
    "start": "2026-10-12T18:30:00+01:00",
    "end": "2026-10-12T19:00:00+01:00",
    "text": "Export 1.7 kW for the flexibility event (Funded 0.85 kWh from the post-event reserve: \u00a30.95 event vs \u00a30.31 buy-back.)",
    "source": "planner-numbers"
   },
   {
    "kind": "v2l",
    "start": "2026-10-12T19:30:00+01:00",
    "end": "2026-10-12T23:00:00+01:00",
    "kwh": 3.44,
    "text": "Refill the house battery with 3.44 kWh from V2L for the reach-next-cheap-slot reservation.",
    "source": "planner",
    "slots": 7
   }
  ]
 }
};
