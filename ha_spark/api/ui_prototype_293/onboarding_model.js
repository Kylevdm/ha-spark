/* PROTOTYPE (#293), throwaway. Onboarding reducer + readiness copied verbatim from
   prototype/257-onboarding-flow (onboarding_logic.html). Pure: no DOM, storage, network. */
const STEP_NAMES = [
  "Who is here", "Integration", "Devices", "Topology and limits",
  "Tariff", "Solar forecast", "EV charger and major loads",
  "Fallback charging schedule", "Review and save", "Finish"
];

function initialState(kind = "fresh") {
  const s = {
    currentStep: 0,
    steps: STEP_NAMES.map(() => "not_started"),
    admin: null,
    integration: { phase: "not_installed", paired: false, adopted: false, versionSkew: false },
    devices: [],
    topology: { banks: [], limitsSet: false, siteImportA: null, siteExportW: null },
    tariff: { answer: "unanswered", rate: null, dispatch: null },
    forecast: { answer: "unanswered", solcastKeySet: false, sites: 0 },
    loads: { answer: "unanswered", ev: null, major: [] },
    fallback: { status: "not_proposed", reason: null, window: null, chargeA: null },
    history: { available: 2, needed: 14 },
    sessionLive: false,
    trustedSoc: false,
    saved: { integration: false, addon: false, integrationRevision: 0, addonRevision: 0 },
    mode: "Observe",
    warnings: [],
    lastEvent: "Fresh add-on install. An administrator can start onboarding.",
    refused: false
  };
  if (kind === "standalone") {
    s.admin = true;
    s.steps[0] = "done";
    s.integration = { phase: "standalone", paired: true, adopted: false, versionSkew: false };
    s.currentStep = 1;
    s.lastEvent = "Existing standalone integration found. Adopt its devices and settings.";
  }
  if (kind === "nonadmin") {
    s.admin = false;
    s.steps[0] = "blocked";
    s.lastEvent = "Read-only: ask a Home Assistant administrator to run onboarding.";
  }
  return s;
}

function copyState(s) { return JSON.parse(JSON.stringify(s)); }
function note(s, message) { if (!s.warnings.includes(message)) s.warnings.push(message); }
function clearNote(s, fragment) { s.warnings = s.warnings.filter(x => !x.includes(fragment)); }
function refuse(previous, message) {
  const s = copyState(previous);
  s.lastEvent = "Refused: " + message;
  s.refused = true;
  return s;
}
function mark(s, index, status = "done") {
  s.steps[index] = status;
  if (status === "done" || status === "skipped") {
    const next = s.steps.findIndex(x => x === "not_started");
    s.currentStep = next < 0 ? 9 : next;
  } else {
    s.currentStep = index;
  }
}
function solis(id) {
  return {
    id, type: "Solis", connection: "RS485-to-TCP gateway",
    bank: null, cache: "unknown", tou: "unknown",
    hold: false, release: false, readback: false, freshness: false,
    chargeA: null, dischargeA: null
  };
}
function alpha(id) {
  return {
    id, type: "AlphaESS", connection: "Modbus TCP",
    bank: null, cache: "not_applicable", tou: "not_applicable",
    hold: false, release: false, readback: false, freshness: false,
    chargeA: null, dischargeA: null
  };
}
function deviceContractOK(d) {
  return d.hold && d.release && d.readback && d.freshness &&
    (d.type !== "Solis" || d.cache === "off");
}
function topologyOK(s) {
  return s.topology.limitsSet && s.devices.length > 0 &&
    s.devices.every(d => d.bank && d.chargeA > 0 && d.dischargeA > 0) &&
    s.topology.banks.every(b => b.chargeA > 0 && b.dischargeA > 0) &&
    s.topology.siteImportA > 0 && s.topology.siteExportW >= 0;
}
function answerComplete(s) {
  return ["done", "skipped"].includes(s.steps[4]) &&
    ["done", "skipped"].includes(s.steps[5]) &&
    ["done", "skipped"].includes(s.steps[6]) &&
    ["done", "skipped"].includes(s.steps[7]);
}
function fallbackCheck(s, variant) {
  if (!topologyOK(s)) return "Set topology and limits first";
  if (variant === "site") return "Fallback charge exceeds the supply current limit";
  if (variant === "bank") return "Fallback charge exceeds a battery-bank current limit";
  if (variant === "export") return "Fallback schedule enables battery export";
  if (variant === "readback") return "Fallback control has no verified read-back";
  if (s.devices.some(d => !d.readback || (d.type === "Solis" && d.cache !== "off"))) {
    return "Fallback control has no trustworthy read-back";
  }
  return null;
}
function fallbackMajorWarning(s) {
  const maxLoad = Math.max(0, ...s.loads.major.map(x => x.amps));
  return maxLoad > 0 && s.fallback.chargeA + maxLoad > s.topology.siteImportA;
}

function onboarding(previous, action) {
  if (!action || typeof action.type !== "string") return refuse(previous, "Unknown action");
  if (action.type === "RESET") return initialState(action.kind || "fresh");
  if (action.type === "ADMIN_NO") {
    const s = initialState("nonadmin");
    return s;
  }
  if (previous.admin === false) return refuse(previous, "This page is read-only. Ask a Home Assistant administrator.");
  const s = copyState(previous);
  s.refused = false;
  const needAdmin = action.type !== "ADMIN_YES";
  if (needAdmin && s.admin !== true) return refuse(previous, "Confirm a Home Assistant administrator first.");

  switch (action.type) {
    case "ADMIN_YES":
      s.admin = true;
      mark(s, 0);
      s.lastEvent = "Administrator confirmed. Integration setup can begin.";
      break;
    case "INTEGRATION_INSTALLED":
      if (s.integration.phase !== "not_installed") return refuse(previous, "Integration installation is already recorded.");
      s.integration.phase = "awaiting_restart";
      mark(s, 1, "in_progress");
      s.lastEvent = "User installed the companion integration through HACS. Restart Home Assistant next.";
      break;
    case "HA_RESTARTED":
      if (s.integration.phase !== "awaiting_restart") return refuse(previous, "Install through HACS before restarting for discovery.");
      s.integration.phase = "discovered";
      s.lastEvent = "Home Assistant restarted and offered the integration through Supervisor discovery.";
      break;
    case "INTEGRATION_FOUND":
      if (s.integration.phase !== "awaiting_restart" && s.integration.phase !== "discovered") {
        return refuse(previous, "Home Assistant must restart after the HACS install.");
      }
      s.integration.phase = "discovered";
      s.lastEvent = "Integration found by Home Assistant. Accept discovery to pair it.";
      break;
    case "PAIR":
      if (s.integration.phase !== "discovered") return refuse(previous, "Accept discovery only after Home Assistant offers the integration.");
      s.integration.phase = "paired";
      s.integration.paired = true;
      mark(s, 1);
      s.lastEvent = "Integration paired through Supervisor discovery.";
      break;
    case "VERSION_SKEW":
      if (!s.integration.paired) return refuse(previous, "Pair or adopt the integration first.");
      s.integration.versionSkew = true;
      note(s, "Repair warning: integration and add-on api_version differ. Onboarding can continue.");
      s.lastEvent = "Version mismatch recorded as a repair warning; pairing remains usable.";
      break;
    case "ADOPT":
      if (s.integration.phase !== "standalone") return refuse(previous, "Adoption applies to an already configured standalone integration.");
      s.integration.adopted = true;
      s.integration.phase = "paired";
      s.devices = [solis("inverter_1")];
      Object.assign(s.devices[0], {
        bank: "bank_1", cache: "off", tou: "V2", hold: true,
        release: true, readback: true, freshness: true, chargeA: 32, dischargeA: 32
      });
      s.topology = {
        banks: [{ id: "bank_1", chargeA: 50, dischargeA: 50, inverters: ["inverter_1"] }],
        limitsSet: true, siteImportA: 80, siteExportW: 0
      };
      mark(s, 1);
      s.steps[2] = "done";
      s.steps[3] = "done";
      s.currentStep = 4;
      s.lastEvent = "Existing standalone devices, bank assignment, and limits adopted. Continue at tariff.";
      break;
    case "DISCOVER_SOLIS":
    case "DISCOVER_ALPHA":
    case "DISCOVER_TWO":
      if (!s.integration.paired) return refuse(previous, "Pair or adopt the integration before discovering devices.");
      s.devices = action.type === "DISCOVER_ALPHA" ? [alpha("inverter_1")] :
        action.type === "DISCOVER_TWO" ? [solis("inverter_1"), solis("inverter_2")] :
        [solis("inverter_1")];
      s.topology = { banks: [], limitsSet: false, siteImportA: null, siteExportW: null };
      mark(s, 2, "in_progress");
      s.lastEvent = s.devices.length + " inverter" + (s.devices.length === 1 ? "" : "s") +
        " found. Probe capabilities, then assign battery banks and limits.";
      break;
    case "NONE_FOUND":
      if (!s.integration.paired) return refuse(previous, "Pair the integration first.");
      s.devices = [];
      mark(s, 2, "in_progress");
      note(s, "No gateway or inverter found. Enter a host manually or retry discovery.");
      s.lastEvent = "No devices found. Manual host entry is available.";
      break;
    case "MANUAL_HOST":
      if (!s.integration.paired) return refuse(previous, "Pair the integration first.");
      s.devices = [solis("inverter_1")];
      clearNote(s, "No gateway");
      mark(s, 2, "in_progress");
      s.lastEvent = "Generic Solis gateway host entered manually. Connection and capability probe remain.";
      break;
    case "CONNECT_FAIL":
      if (s.devices.length === 0) return refuse(previous, "Discover or enter a device first.");
      mark(s, 2, "failed");
      note(s, "Device connection failed. Check host and gateway, then retry discovery.");
      s.lastEvent = "Connection failed; no device capability is trusted.";
      break;
    case "CACHE_ON":
    case "CACHE_OFF":
      if (!s.devices.some(d => d.type === "Solis")) return refuse(previous, "A Solis gateway is needed for a caching check.");
      s.devices.filter(d => d.type === "Solis").forEach(d => {
        d.cache = action.type === "CACHE_ON" ? "on" : "off";
        if (d.cache === "on") d.readback = false;
      });
      if (action.type === "CACHE_ON") {
        note(s, "Gateway caching is on: repeated-read timing indicates cached read-back.");
        s.lastEvent = "Repeated-read timing detected gateway storage (caching) mode ON.";
      } else {
        clearNote(s, "Gateway caching is on");
        s.lastEvent = "Owner turned gateway storage (caching) mode off. Re-probe read-back.";
      }
      break;
    case "PROBE_OK":
      if (s.devices.length === 0) return refuse(previous, "Discover or enter a device first.");
      s.devices.forEach(d => {
        d.tou = d.type === "Solis" ? "V2 (register 33289)" : "not_applicable";
        d.hold = true;
        d.release = true;
        d.readback = d.type !== "Solis" || d.cache === "off";
        d.freshness = true;
      });
      mark(s, 2);
      s.lastEvent = "Probe recorded hold, release, read-back, freshness, and detected firmware charge-slot path. No firmware choice was asked.";
      break;
    case "PROBE_GAP":
      if (s.devices.length === 0) return refuse(previous, "Discover or enter a device first.");
      s.devices.forEach(d => {
        d.tou = d.type === "Solis" ? "V1 (register 33289)" : "not_applicable";
        d.hold = false;
        d.release = true;
        d.readback = d.type !== "Solis" || d.cache === "off";
        d.freshness = false;
      });
      mark(s, 2);
      note(s, "Capability gap: hold or fresh protection evidence has not been validated.");
      s.lastEvent = "Probe completed with capability gaps. Observe remains available.";
      break;
    case "REPROBE":
      if (s.devices.length === 0) return refuse(previous, "No devices to re-probe.");
      s.devices.forEach(d => {
        d.hold = true; d.release = true; d.freshness = true;
        d.readback = d.type !== "Solis" || d.cache === "off";
        d.tou = d.type === "Solis" ? "V2 (register 33289)" : "not_applicable";
      });
      clearNote(s, "Capability gap");
      s.lastEvent = "Capabilities re-probed. Readiness uses the new evidence.";
      break;
    case "LIMITS_ONE":
    case "LIMITS_SHARED":
      if (s.devices.length === 0) return refuse(previous, "Discover devices before assigning banks.");
      if (action.type === "LIMITS_SHARED" && s.devices.length < 2) return refuse(previous, "Discover two inverters first.");
      s.devices.forEach(d => {
        d.bank = "bank_1";
        d.chargeA = 32;
        d.dischargeA = 32;
      });
      s.topology = {
        banks: [{
          id: "bank_1", chargeA: action.type === "LIMITS_SHARED" ? 50 : 40,
          dischargeA: action.type === "LIMITS_SHARED" ? 50 : 40,
          inverters: s.devices.map(d => d.id)
        }],
        limitsSet: true, siteImportA: 80, siteExportW: 0
      };
      mark(s, 3);
      s.lastEvent = "Preset-filled per-inverter, battery-bank, supply current, and site export limits confirmed.";
      break;
    case "TARIFF_FOUND":
      s.tariff = {
        answer: "confirmed",
        rate: "sensor.octopus_current_rate",
        dispatch: "binary_sensor.octopus_dispatch"
      };
      mark(s, 4);
      s.lastEvent = "Octopus Energy rate and dispatch entity proposals confirmed.";
      break;
    case "TARIFF_SKIP":
      s.tariff = { answer: "skipped", rate: null, dispatch: null };
      mark(s, 4, "skipped");
      note(s, "Octopus Energy integration absent: the planner cannot price tariff slots.");
      s.lastEvent = "Tariff skipped. Optimise is unavailable until a tariff is confirmed.";
      break;
    case "SOLCAST_SET":
      s.forecast = { answer: "confirmed", solcastKeySet: true, sites: action.sites === 2 ? 2 : 1 };
      mark(s, 5);
      s.lastEvent = "Native Solcast client configured for " + s.forecast.sites +
        " site(s). API key is recorded only as set.";
      break;
    case "SOLCAST_SKIP":
      s.forecast = { answer: "skipped", solcastKeySet: false, sites: 0 };
      mark(s, 5, "skipped");
      s.lastEvent = "Solar forecast skipped. Planning quality may be lower.";
      break;
    case "LOADS_EV":
      s.loads = {
        answer: "confirmed",
        ev: {
          plug: "sensor.ev_charger_plug",
          status: "sensor.ev_charger_status",
          power: "sensor.ev_charger_power"
        },
        major: [{ name: "Car charger", amps: 32, power: "sensor.ev_charger_power" }]
      };
      mark(s, 6);
      s.lastEvent = "EV plug, status, power, and car-charger demand confirmed.";
      break;
    case "LOADS_HEATPUMP":
      s.loads = {
        answer: "confirmed",
        ev: null,
        major: [
          { name: "Heat pump heating/cooling", amps: 55, power: "sensor.heat_pump_power" },
          { name: "Induction hob", amps: 32, power: "sensor.induction_hob_power" }
        ]
      };
      mark(s, 6);
      s.lastEvent = "Heat pump and induction-hob power proposals and demand confirmed.";
      break;
    case "LOADS_NONE":
      s.loads = { answer: "none", ev: null, major: [] };
      mark(s, 6);
      s.lastEvent = "No EV charger or major loads recorded.";
      break;
    case "FALLBACK_PROPOSE":
      if (!topologyOK(s)) return refuse(previous, "Set device, bank, and site limits first.");
      s.fallback = {
        status: "proposed", reason: null,
        window: s.tariff.answer === "confirmed" ? "00:30–04:30" : "01:00–04:00",
        chargeA: s.devices.length > 1 ? 30 : 20
      };
      mark(s, 7, "in_progress");
      s.lastEvent = "Conservative fallback charging window proposed from limits" +
        (s.tariff.answer === "confirmed" ? " and Octopus rates." : ". No tariff price is available.");
      break;
    case "FALLBACK_VALIDATE": {
      if (s.fallback.status !== "proposed" && s.fallback.status !== "rejected") {
        return refuse(previous, "Propose a fallback schedule first.");
      }
      const error = fallbackCheck(s, "valid");
      if (error) {
        s.fallback.status = "rejected";
        s.fallback.reason = error;
        mark(s, 7, "failed");
        s.lastEvent = "Fallback rejected at save validation: " + error + ".";
      } else {
        s.fallback.status = "valid";
        s.fallback.reason = null;
        mark(s, 7);
        if (fallbackMajorWarning(s)) {
          note(s, "Fallback plus the largest major load may exceed the supply current limit; the supply guard must respond live.");
        }
        s.lastEvent = "Fallback schedule validated: charge only, within inverter, bank, and site limits, with read-back.";
      }
      break;
    }
    case "FALLBACK_REJECT": {
      if (!topologyOK(s)) return refuse(previous, "Set topology and limits first.");
      const reason = fallbackCheck(s, action.reason || "site");
      s.fallback = { status: "rejected", reason, window: "01:00–04:00", chargeA: 90 };
      mark(s, 7, "failed");
      s.lastEvent = "Fallback rejected at integration save validation: " + reason + ".";
      break;
    }
    case "FALLBACK_DEFER":
      s.fallback = { status: "deferred", reason: null, window: null, chargeA: null };
      mark(s, 7, "skipped");
      s.lastEvent = "Fallback deferred. Onboarding can finish in Observe; Optimise will remain unavailable.";
      break;
    case "SAVE_INTEGRATION":
      if (!s.integration.paired || !topologyOK(s)) {
        return refuse(previous, "Pair the integration and confirm topology and limits first.");
      }
      if (!answerComplete(s)) return refuse(previous, "Answer or skip tariff, forecast, loads, and fallback before review.");
      if (s.fallback.status === "rejected") return refuse(previous, "The integration rejects the invalid fallback schedule. Fix or defer it.");
      s.saved.integration = true;
      s.saved.integrationRevision++;
      s.steps[8] = "in_progress";
      s.currentStep = 8;
      s.lastEvent = "Integration settings saved first: connections, topology, limits, and " +
        (s.fallback.status === "valid" ? "validated fallback." : "no fallback yet.");
      break;
    case "SAVE_ADDON":
      if (!s.saved.integration) return refuse(previous, "Integration settings must save first.");
      s.saved.addon = true;
      s.saved.addonRevision++;
      mark(s, 8);
      s.lastEvent = "Add-on planning, tariff, forecast-consumer, and UI settings saved. Operating mode stayed Observe.";
      break;
    case "ADDON_SAVE_FAIL":
      if (!s.saved.integration || s.saved.addon) return refuse(previous, "Simulate this failure after integration save and before add-on save.");
      s.steps[8] = "failed";
      s.currentStep = 8;
      note(s, "Partial setup: integration settings saved, add-on settings failed. Retry add-on save.");
      s.lastEvent = "Add-on save failed after integration save. The integration revision remains durable.";
      break;
    case "RELOAD":
      if (!s.saved.integration || s.saved.addon) return refuse(previous, "Reload repair is useful only for a half-saved setup.");
      s.steps[8] = "in_progress";
      s.currentStep = 8;
      s.lastEvent = "Page reloaded. Integration revision was read back; add-on settings are still unsaved. Resume at add-on save.";
      break;
    case "FINISH":
      if (!s.saved.integration || !s.saved.addon) return refuse(previous, "Save both stores before finishing.");
      s.mode = "Observe";
      s.sessionLive = true;
      s.trustedSoc = true;
      mark(s, 9);
      s.currentStep = 9;
      s.lastEvent = "Onboarding complete in Observe. No hardware writes. An administrator may separately choose an operating mode.";
      break;
    case "SESSION_LOST":
      s.sessionLive = false;
      s.lastEvent = "Add-on session lost. Optimise is unavailable until a live session returns.";
      break;
    case "SESSION_RESTORED":
      s.sessionLive = true;
      s.lastEvent = "Live add-on session restored.";
      break;
    case "SOC_UNTRUSTED":
      s.trustedSoc = false;
      s.lastEvent = "SoC is untrusted. Optimise is unavailable; fixed fallback does not depend on SoC.";
      break;
    case "SOC_TRUSTED":
      s.trustedSoc = true;
      s.lastEvent = "SoC integrity is trusted again.";
      break;
    case "HISTORY_READY":
      s.history.available = s.history.needed;
      s.lastEvent = "Enough derived base-load history is available for the forecast.";
      break;
    default:
      return refuse(previous, "Unknown action: " + action.type);
  }
  return s;
}

function readiness(s) {
  const protect = [];
  const optimise = [];
  const advisory = [];
  if (!s.saved.integration) protect.push("Integration settings have not been saved");
  if (s.devices.length === 0) protect.push("No inverter has been discovered or adopted");
  for (const d of s.devices) {
    if (d.type === "Solis" && d.cache === "on") {
      protect.push("Gateway caching is on — read-back can't be trusted (" + d.id + ")");
    } else if (d.type === "Solis" && d.cache !== "off") {
      protect.push("Gateway caching has not been ruled out (" + d.id + ")");
    }
    if (!d.hold) protect.push("Hold command is not validated (" + d.id + ")");
    if (!d.release) protect.push("Hold release is not validated (" + d.id + ")");
    if (!d.readback) protect.push("Control read-back is not validated (" + d.id + ")");
    if (!d.freshness) protect.push("Fresh protection evidence contract is not validated (" + d.id + ")");
  }
  optimise.push(...protect);
  if (!s.saved.addon) optimise.push("Add-on settings have not been saved");
  if (s.fallback.status !== "valid") optimise.push("No validated fallback charging schedule yet");
  if (!s.sessionLive) optimise.push("No live add-on session");
  if (!s.trustedSoc) optimise.push("SoC is not trusted");
  if (s.history.available < s.history.needed) {
    advisory.push("Derived base-load history: " + s.history.available + " of " +
      s.history.needed + " days needed for the forecast");
  }
  if (s.tariff.answer !== "confirmed") {
    optimise.push("No tariff confirmed — the planner cannot price slots");
  }
  if (s.forecast.answer !== "confirmed") advisory.push("No Solcast forecast configured");
  if (s.integration.versionSkew) advisory.push("Repair warning: integration/add-on api_version mismatch");
  if (s.fallback.status === "valid" && fallbackMajorWarning(s)) {
    advisory.push("Fallback plus the largest major load may exceed the supply current limit");
  }
  advisory.push("Whole-Home Assistant outages cannot guarantee active protection; device behavior needs model-specific validation.");
  return {
    Protect: { ready: protect.length === 0, unmet: protect, advisory },
    Optimise: { ready: optimise.length === 0, unmet: optimise, advisory }
  };
}

window.ONBOARDING = { STEP_NAMES, initialState, onboarding, readiness };
