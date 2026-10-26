// The today-so-far module renders recorded history, never invented values.
import {html, useEffect, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {clock, number} from "./axis.js";

const MEASURED_UNAVAILABLE = {
    no_entity: "Measured solar is unavailable: no solar generation statistic is configured.",
    no_statistics: "Measured solar is unavailable: no five-minute statistics were found.",
    unsupported_unit: "Measured solar is unavailable: the statistic unit is unsupported.",
    read_failed: "Measured solar is unavailable: the statistics read failed.",
};

// Omitting `from` asks the server for the household's own midnight, so the
// browser never guesses the offset (it differs from now's on DST days).
async function fetchHistory(to) {
    const range = to ? `&to=${encodeURIComponent(to)}` : "";
    const response = await fetch(`api/history?resolution=slot${range}`, {cache: "no-store"});
    return response.ok ? await response.json() : null;
}

export function TodaySpan({timeline}) {
    const [payload, setPayload] = useState(null);
    const [failed, setFailed] = useState(false);
    // Without a plan the span runs to now (the server's default `to`).
    const to = timeline?.horizon_start ?? null;
    useEffect(() => {
        let active = true;
        setPayload(null);
        setFailed(false);
        async function load() {
            try {
                const result = await fetchHistory(to);
                if (result === null) throw new Error("history unavailable");
                if (active) setPayload(result);
            } catch {
                if (active) setFailed(true);
            }
        }
        void load();
        return () => { active = false; };
    }, [to, timeline?.now]);
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
        ${reason ? html`<p role="status">${note}</p>` : html`<p>Measured solar so far:
            ${number(payload.totals.solar_measured_kwh)} kWh</p>`}`;
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
    const values = buckets.flatMap(bucket =>
        [bucket.solar_forecast_kwh, bucket.solar_measured_kwh]);
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
    const solid = [];
    drawing = false;
    for (const bucket of buckets) {
        const value = bucket.solar_measured_kwh;
        if (value == null) {
            drawing = false;
            continue;
        }
        const x0 = x(bucket.start);
        solid.push(`${drawing ? "L" : "M"}${x0},${y(value)} L${x0 + slotWidth},${y(value)}`);
        drawing = true;
    }
    const nowX = timeline ? Math.min(x(timeline.now), right) : right;
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
                ${solid.length ? html`<path class="series solar_measured_kwh"
                    d=${solid.join(" ")}/>` : null}
                <line class="now-marker" x1=${nowX} x2=${nowX} y1="24" y2="110"/>
                <text class="now-label" x=${nowX} y="18">now</text>
            </svg>
        </div>
    </section>`;
}
