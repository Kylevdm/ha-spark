// The today-so-far module renders recorded history, never invented values.
import {html, useEffect, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {clock, number} from "./axis.js";

// Ticket 10 replaces these fixed lines with the measured series and its codes.
const MEASURED_UNAVAILABLE = {
    no_entity: "Measured solar is unavailable: no solar generation statistic is configured.",
    not_implemented: "Measured solar is not available in this version yet.",
};

// Local midnight on the household clock, in the offset the timeline reported
// for `instant` (the browser's zone is not the household's). On the
// spring-forward night that lands an hour before local midnight; the fetch
// below retries an hour later rather than giving up for the whole day.
function midnightOf(instant) {
    return instant.slice(0, 10) + "T00:00:00" + instant.slice(19);
}

function plusAnHour(instant) {
    return new Date(Date.parse(instant) + 3600000).toISOString();
}

async function fetchHistory(from, to) {
    const response = await fetch(
        `api/history?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
            + "&resolution=slot",
        {cache: "no-store"},
    );
    return response.ok ? await response.json() : null;
}

export function TodaySpan({timeline}) {
    const [payload, setPayload] = useState(null);
    const [failed, setFailed] = useState(false);
    const to = timeline.horizon_start ?? timeline.now;
    const from = midnightOf(timeline.now);
    useEffect(() => {
        let active = true;
        setPayload(null);
        setFailed(false);
        async function load() {
            try {
                let result = await fetchHistory(from, to);
                if (result === null) result = await fetchHistory(plusAnHour(from), to);
                if (result === null) throw new Error("history unavailable");
                if (active) setPayload(result);
            } catch {
                if (active) setFailed(true);
            }
        }
        void load();
        return () => { active = false; };
    }, [from, to]);
    if (failed) return html`<section aria-label="Today so far">
        <h2>Today so far</h2>
        <p>History unavailable. Reload to try again.</p>
    </section>`;
    if (!payload) return html`<section aria-label="Today so far">
        <h2>Today so far</h2>
        <p>Loading history…</p>
    </section>`;
    const reason = payload.unavailable?.solar_measured;
    const note = MEASURED_UNAVAILABLE[reason] ?? "Measured solar is unavailable.";
    const buckets = payload.buckets;
    const header = html`<h2>Today so far</h2>
        <p>Solar forecast so far: ${number(payload.totals.solar_forecast_kwh)} kWh</p>
        <p role="status">${note}</p>`;
    if (!buckets.length) return html`<section aria-label="Today so far">${header}</section>`;
    // Step geometry only: recorded buckets are drawn, gaps stay gaps.
    const step = 30 * 60000;
    const width = Math.max(960, buckets.length * 24 + 140);
    const left = 130;
    const right = width - 20;
    const first = Date.parse(buckets[0].start);
    const last = Date.parse(buckets[buckets.length - 1].start) + step;
    const x = at => left + (Date.parse(at) - first) / (last - first) * (right - left);
    const slotWidth = step / (last - first) * (right - left);
    const values = buckets.map(bucket => bucket.solar_forecast_kwh);
    const max = Math.max(0, ...values.filter(value => value != null));
    const y = value => 105 - (max > 0 ? value / max * 65 : 0);
    const dashed = [];
    let drawing = false;
    for (const bucket of buckets) {
        const value = bucket.solar_forecast_kwh;
        if (value == null) {
            drawing = false;
            continue;
        }
        const x0 = x(bucket.start);
        dashed.push(`${drawing ? "L" : "M"}${x0},${y(value)} L${x0 + slotWidth},${y(value)}`);
        drawing = true;
    }
    const nowX = Math.min(x(timeline.now), right);
    return html`<section aria-label="Today so far">${header}
        <div class="chart-scroll" tabindex="0" aria-label="Scrollable history chart">
            <svg viewBox=${`0 0 ${width} 150`} width=${width} height="150"
                 role="group" aria-label="Half-hour solar forecast so far">
                <text class="lane-label" x="4" y="20">Solar kWh</text>
                ${buckets.map((bucket, i) => i % 6 === 0 ? html`<g key=${bucket.start}>
                    <line class="slot-grid" x1=${x(bucket.start)} x2=${x(bucket.start)}
                        y1="30" y2="110"/>
                    <text class="axis-label" x=${x(bucket.start)} y="134">
                        ${clock(bucket.start)}</text>
                </g>` : null)}
                ${dashed.length ? html`<path class="series solar_raw_kwh" d=${dashed.join(" ")}/>`
                    : null}
                <line class="now-marker" x1=${nowX} x2=${nowX} y1="24" y2="110"/>
                <text class="now-label" x=${nowX} y="18">now</text>
            </svg>
        </div>
    </section>`;
}
