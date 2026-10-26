// Text summary of recorded history; all chart geometry lives on the shared axis.
import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {number} from "./axis.js";

const MEASURED_UNAVAILABLE = {
    no_entity: "Measured solar is unavailable: no solar generation statistic is configured.",
    no_statistics: "Measured solar is unavailable: no five-minute statistics were found.",
    unsupported_unit: "Measured solar is unavailable: the statistic unit is unsupported.",
    read_failed: "Measured solar is unavailable: the statistics read failed.",
};

export function TodaySpan({payload, failed, hasToday}) {
    if (!hasToday) return html`<section aria-label="Today so far">
        <h2>Today so far</h2><p>Today so far appears after the next plan.</p>
    </section>`;
    if (failed) return html`<section aria-label="Today so far">
        <h2>Today so far</h2><p>History unavailable. Reload to try again.</p>
    </section>`;
    if (!payload) return html`<section aria-label="Today so far">
        <h2>Today so far</h2><p>Loading history…</p>
    </section>`;
    const reason = payload.unavailable?.solar_measured;
    const note = MEASURED_UNAVAILABLE[reason] ?? "Measured solar is unavailable.";
    return html`<section aria-label="Today so far">
        <h2>Today so far</h2>
        <p>Solar forecast so far: ${number(payload.totals.solar_forecast_kwh)} kWh</p>
        ${reason ? html`<p role="status">${note}</p>` : html`<p>Measured solar so far:
            ${number(payload.totals.solar_measured_kwh)} kWh</p>`}
    </section>`;
}
