// Read-only Statistics view. The server owns household dates, pricing and coverage.
import {html, useEffect, useState} from "./vendor/htm-3.1.1-preact-standalone.module.js";
import {clock, number, slotGrid, SlotGridLines} from "./plan/axis.js";
import {steps} from "./plan/lanes.js";

const ranges = {day: "Day", week: "Week", month: "Month", year: "Year", all: "All time"};
const flows = {
    grid_import: "Grid import", grid_export: "Grid export", solar_generation: "Solar generation",
    battery_charge: "Battery charge", battery_discharge: "Battery discharge",
    ev_charge: "EV charge", v2l: "V2L",
};
const derived = {house_load_kwh: "House load", base_load_kwh: "Base load"};
const costs = {
    actual_cost: "Actual cost", no_battery_cost: "No-battery cost", battery_savings: "Battery savings",
};
const series = {...flows, ...derived, ...costs};
const provenance = {
    boundary: "Boundary measurement", statistics_fill: "Statistics fill",
    distributed: "Distributed across missed boundaries", hourly_backfill: "Lower resolution (hourly)",
    absent: "Gap",
};
const sessions = {free_electricity: "Free Electricity", happy_hour: "Happy Hour"};
const halfHour = 30 * 60000;

const energy = value => value == null ? "—" : `${number(value, 3)} kWh`;
const money = value => value == null ? "—" : `£${number(value)}`;
const percent = value => value == null ? "—" : `${number(value * 100, 1)}%`;
const session = bucket => sessions[bucket.cost_inputs?.import_session];
const formatValue = (field, value) => (Object.hasOwn(costs, field) ? money : energy)(value);

// Shift a calendar label, not an instant in the browser's time zone.
function stepDate({kind, at}, direction) {
    const day = new Date(`${at}T00:00:00Z`);
    if (kind === "year") day.setUTCFullYear(day.getUTCFullYear() + direction, 0, 1);
    else if (kind === "month") day.setUTCMonth(day.getUTCMonth() + direction, 1);
    else if (kind === "week") {
        day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7 + direction * 7);
    } else day.setUTCDate(day.getUTCDate() + direction);
    return day.toISOString().slice(0, 10);
}

function Metrics({entries, cards = false}) {
    return html`<dl class=${cards ? "plan-costs" : "stats-metrics"}>
        ${entries.map(([label, value]) => html`<div key=${label}><dt>${label}</dt><dd>${value}</dd></div>`)}
    </dl>`;
}

function Coverage({totals, soFar = false}) {
    const {covered_slots, priced_slots, total_slots, energy_coverage, cost_coverage} = totals;
    return html`<p><strong>Coverage${soFar ? " so far" : ""}</strong> ·
        Energy: ${covered_slots}/${total_slots} slots (${percent(energy_coverage)}) ·
        Cost: ${priced_slots}/${total_slots} slots (${percent(cost_coverage)})
        ${totals.lower_resolution ? " · Lower resolution (hourly)" : ""}
    </p>`;
}

function valueAt(bucket, field, day = false) {
    if (Object.hasOwn(flows, field)) {
        const flow = bucket.flows?.[field];
        // An absent flow is a gap, whatever a stored kWh says.
        return day ? (flow?.provenance === "absent" ? null : flow?.kwh)
            : bucket.components_kwh[field];
    }
    return bucket[field];
}

function flowEntries(bucket, day = false) {
    return Object.entries({...flows, ...derived}).map(([field, label]) => {
        const origin = Object.hasOwn(flows, field) && day
            ? ` · ${provenance[bucket.flows?.[field]?.provenance] ?? "Gap"}` : "";
        return [label, energy(valueAt(bucket, field, day)) + origin];
    });
}

function costEntries(bucket) {
    return Object.entries(costs).map(([field, label]) => [label, money(bucket[field])]);
}

function slotMarks(bucket) {
    const kinds = new Set(Object.values(bucket.flows ?? {}).map(flow => flow.provenance));
    return [bucket.covered_slots === 0 && "G", kinds.has("statistics_fill") && "F",
        kinds.has("distributed") && "D", bucket.lower_resolution && "H",
        bucket.actual_cost == null && "U"].filter(Boolean);
}

function HistoryChart({payload, field, selected, select}) {
    const {buckets, resolution} = payload;
    if (!buckets.length) return html`<p>No half-hours in this period yet.</p>`;
    const day = resolution === "slot";
    const data = buckets.map(bucket => ({start: bucket.start, value: valueAt(bucket, field, day)}));
    const grid = day ? slotGrid(Date.parse(data[0].start),
        Date.parse(data.at(-1).start) + halfHour, halfHour) : null;
    const width = grid?.width ?? Math.max(560, data.length * 38 + 90);
    const left = grid?.left ?? 65;
    const right = grid?.right ?? width - 20;
    const slotWidth = grid?.slotWidth ?? (right - left) / data.length;
    const x = (at, i) => grid ? grid.x(at) : left + i * slotWidth;
    const [min, max] = data.reduce(([lo, hi], {value}) => value == null
        ? [lo, hi] : [Math.min(lo, value), Math.max(hi, value)], [0, 0]);
    const scale = Math.max(1, -min, max);
    const span = (max / scale - min / scale) || 1;
    const y = value => 146 - (value / scale - min / scale) / span * 116;
    const height = day ? 255 : 196;
    return html`<div class="chart-scroll" tabindex="0" aria-label="Scrollable history chart">
        <svg width=${width} height=${height} viewBox=${`0 0 ${width} ${height}`}
             role="group" aria-label=${series[field]}>
            ${grid ? html`<${SlotGridLines} grid=${grid}
                labels=${new Map(data.map(({start}) => [Date.parse(start), start]))}
                top=${24} bottom=${222} labelY=${234}/>` : null}
            <text x="4" y="18" class="axis-label">${Object.hasOwn(costs, field) ? "GBP" : "kWh"}</text>
            <text x="4" y="30" class="axis-label">${number(max)}</text>
            <text x="4" y="146" class="axis-label">${number(min)}</text>
            <line class="lane-rule" x1=${left} x2=${right} y1=${y(0)} y2=${y(0)}/>
            ${day ? html`<path class="series stats-series"
                d=${steps(data, "value", grid.x, slotWidth, y)}/>` : null}
            ${data.map(({start, value}, i) => {
                const label = day ? clock(start) : start.slice(0, resolution === "month" ? 7 : 10);
                const bucket = buckets[i];
                const at = x(start, i);
                const marks = day ? slotMarks(bucket) : [];
                const description = [label, value == null ? "Gap" : formatValue(field, value),
                    session(bucket)].filter(Boolean).join(" · ");
                return html`<g key=${start}>
                    ${!day && value != null ? html`<rect class="stats-bar"
                        x=${at + slotWidth * .18} width=${slotWidth * .64}
                        y=${Math.min(y(value), y(0))} height=${Math.max(1, Math.abs(y(value) - y(0)))}/>`
                        : null}
                    ${!day && i % Math.max(1, Math.ceil(78 / slotWidth)) === 0
                        ? html`<text class="axis-label" x=${at} y="180">${label}</text>` : null}
                    ${marks.map(mark => html`<text class="axis-label" x=${at + 5}
                        y=${166 + "GFDHU".indexOf(mark) * 12}>${mark}</text>`)}
                    <rect class=${selected === i ? "slot-hit selected" : "slot-hit"}
                        x=${at} y="24" width=${slotWidth} height=${day ? 198 : 136}
                        tabindex="0" role="button" aria-label=${description} aria-pressed=${selected === i}
                        onClick=${() => select(i)} onKeyDown=${event => {
                            if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                select(i);
                            }
                        }}><title>${description}</title></rect>
                </g>`;
            })}
        </svg>
    </div>`;
}

function SlotInputs({bucket, solar}) {
    const inputs = bucket.cost_inputs;
    return html`
        ${bucket.negative_clamped ? html`<p>Negative derived load was clamped.</p>` : null}
        <p>Free session: ${session(bucket) ?? "None priced"}</p>
        ${inputs ? html`<${Metrics} entries=${[
            ["Import rate", `£${number(inputs.import_rate, 4)}/kWh`],
            ["Export rate", `£${number(inputs.export_rate, 4)}/kWh`],
            ["Grid import used", energy(inputs.grid_import_kwh)],
            ["Grid export used", energy(inputs.grid_export_kwh)],
            ["House load used", energy(inputs.house_load_kwh)], ["Solar used", energy(inputs.solar_kwh)],
            ["Free import", energy(inputs.free_import_kwh)],
            ["No-battery import", energy(inputs.no_battery_import_kwh)],
            ["No-battery export", energy(inputs.no_battery_export_kwh)],
            ["No-battery free import", energy(inputs.no_battery_free_import_kwh)],
            ["Axle earnings (estimated)", money(inputs.axle_earnings)],
        ]}/>` : null}
        ${solar ? html`<h4>Evening-plan solar forecast vs measured</h4><${Metrics} entries=${[
            ["P10 forecast", energy(solar.p10_kwh)], ["P50 forecast", energy(solar.p50_kwh)],
            ["P90 forecast", energy(solar.p90_kwh)], ["Measured solar", energy(solar.measured_kwh)],
            ["Forecast − measured", energy(solar.error_kwh)],
            ["P10–P90 band", solar.band_hit == null ? "—" : solar.band_hit ? "Hit" : "Miss"],
        ]}/>` : null}`;
}

function DaySlots({buckets, solar}) {
    return html`<details>
        <summary>Half-hour audit: flows, pricing and forecast inputs</summary>
        ${buckets.map((bucket, i) => html`<details key=${bucket.start}>
            <summary>${clock(bucket.start)} · ${money(bucket.actual_cost)}
                ${session(bucket) ? ` · ${session(bucket)}` : ""}
                ${bucket.covered_slots ? "" : " · Gap"}
                ${bucket.actual_cost == null ? " · Unpriced" : ""}</summary>
            <${BucketDetails} bucket=${bucket} day=${true} solar=${solar?.slots?.[i]}/>
        </details>`)}
    </details>`;
}

function BucketDetails({bucket, day, solar, drill}) {
    return html`<div class="slot-details">
        <${Coverage} totals=${bucket}/>
        ${bucket.actual_cost == null && day ? html`<p>Unpriced:
            ${bucket.cost_reason?.replaceAll("_", " ") ?? "unknown"}</p>` : null}
        <${Metrics} entries=${[...flowEntries(bucket, day), ...costEntries(bucket)]}/>
        ${day ? html`<${SlotInputs} bucket=${bucket} solar=${solar}/>`
            : drill ? html`<button onClick=${drill}>Open this period</button>` : null}
    </div>`;
}

function Costs() {
    return html`<details>
        <summary>How costs and Battery savings are calculated</summary>
        <p>Actual cost = (import - free import) × import rate - export × export rate
            - Axle earnings (estimated)</p>
        <p>No-battery cost = (max(0, house - solar) - no-battery free import) × import rate
            - max(0, solar - house) × export rate</p>
        <p>Battery savings = no-battery cost - actual cost (the battery and ha-spark together)</p>
        <p>Standing charges excluded. Free Electricity covers all import; Happy Hour covers up to
            16 kWh per booked hour, tracked separately for actual and no-battery import.</p>
        <p>Open a Day to see its half-hour inputs and captured rates.</p>
    </details>`;
}

function Axle({axle}) {
    const earnings = axle?.earnings_by_day.filter(day => day.earnings_gbp !== 0) ?? [];
    return html`<section>
        <h2>Axle</h2>
        ${!axle ? html`<p>Axle history unavailable.</p>` : html`
            <p>Axle delivery rate: ${percent(axle.delivery_rate)}</p>
            <h3>Axle earnings (estimated)</h3>
            ${earnings.length ? html`<ul>
                ${earnings.map(({date, earnings_gbp}) => html`
                    <li key=${date}>${date}: ${earnings_gbp == null ? "No estimate" : money(earnings_gbp)}</li>`)}
            </ul>` : html`<p>No recorded earnings in this range.</p>`}
            ${axle.events.length ? html`<details><summary>Event outcomes</summary><ul>
                ${axle.events.map(event => html`<li>${clock(event.start)} to ${clock(event.end)} ·
                    ${event.outcome} · Delivered ${energy(event.delivered_kwh)} ·
                    ${event.earnings_gbp == null ? "No estimate" : money(event.earnings_gbp)}</li>`)}
            </ul></details>` : null}`}
    </section>`;
}

function Accuracy({solar, load, meter}) {
    return html`<section>
        <h2>Forecast accuracy</h2>
        <h3>Evening-plan solar accuracy</h3>
        ${solar ? html`<p>MAE: ${energy(solar.mae_kwh)} · Bias (forecast − measured):
            ${energy(solar.bias_kwh)} · Solar coverage: ${solar.scored_slots}/${solar.total} slots
            (${percent(solar.coverage)})${solar.reason ? ` · ${solar.reason}` : ""}</p>`
            : html`<p>Evening-plan forecast unavailable.</p>`}
        <h3>Load accuracy (vs derived base load)</h3>
        ${!load ? html`<p>Load forecast history unavailable.</p>` : !load.models.length
            ? html`<p>No recorded forecasts.</p>` : html`
            <ul>${load.models.map(model => html`<li key=${model.model}><strong>${model.model}</strong>:
                Daily MAE ${energy(model.daily_mae_kwh)} (${model.days} days) ·
                Shape MAE ${energy(model.shape_mae_kwh)} (${model.shape_slots} slots)</li>`)}</ul>
            <details><summary>Daily error: active model and median</summary><ul>
                ${load.days.map(day => html`<li key=${day.day}>${day.day} · ${day.model}:
                    ${energy(day.error_kwh)} · median:
                    ${energy(day.model === "median" ? day.error_kwh : day.median_error_kwh)}
                    (${day.covered_slots}/${day.total_slots} slots)</li>`)}
            </ul></details>`}
        <h2>Meter reconciliation</h2>
        ${meter ? html`<p>Ledger import vs billed import: ${percent(meter.range_fraction)}
            (${meter.covered_days}/${Object.keys(meter.by_day).length} days covered)</p>
            <details><summary>Daily reconciliation</summary><ul>
                ${Object.entries(meter.by_day).map(([date, fraction]) => html`
                    <li key=${date}>${date}: ${percent(fraction)}</li>`)}
            </ul></details>` : html`<p>Meter history unavailable.</p>`}
    </section>`;
}

function Results({payload, changeRange}) {
    const [field, setField] = useState("grid_import");
    const [selected, select] = useState(null);
    const {range, resolution, range_totals: totals, buckets, provenance_counts: counts} = payload;
    const day = resolution === "slot";
    const bucket = buckets[selected];
    return html`
        <p>Household clock · ${clock(range.from)} to ${clock(range.to)}</p>
        <${Coverage} totals=${totals} soFar=${range.kind === "all" || stepDate(range, 1) > range.today}/>
        ${payload.unavailable.statistics ? html`<p>Energy ledger statistics unavailable.</p>` : null}
        ${counts ? html`<p>Grid import provenance · Statistics fill: ${counts.statistics_fill ?? 0} ·
            Distributed: ${counts.distributed ?? 0} · Lower resolution (hourly):
            ${counts.hourly_backfill ?? 0} · Gaps: ${(counts.gap ?? 0) + (counts.absent ?? 0)}</p>`
            : html`<p>Grid import provenance unavailable.</p>`}
        <h2>Energy flows</h2>
        <${Metrics} entries=${flowEntries(totals)} cards=${true}/>
        <h2>Costs</h2>
        <${Metrics} entries=${costEntries(totals)} cards=${true}/>
        <p>Battery savings credit the battery and ha-spark together.</p>
        <${Costs}/>
        <h2>${day ? "Half-hour history" : resolution === "day" ? "Daily history" : "Monthly history"}</h2>
        <label>Show <select value=${field} onChange=${event => setField(event.currentTarget.value)}>
            ${Object.entries(series).map(([key, label]) => html`<option key=${key} value=${key}>${label}</option>`)}
        </select></label>
        <p>Select a ${day ? "half-hour" : "bar"} for its numbers. A dash means unavailable, not zero.</p>
        ${day ? html`<p>G: gap · F: statistics fill · D: distributed · H: lower resolution (hourly) · U: unpriced</p>` : null}
        <${HistoryChart} payload=${payload} field=${field} selected=${selected} select=${select}/>
        ${bucket ? html`<section aria-live="polite">
            <h3>${day ? clock(bucket.start) : bucket.start.slice(0, resolution === "month" ? 7 : 10)}</h3>
            <${BucketDetails} bucket=${bucket} day=${day} solar=${payload.solar_accuracy?.slots?.[selected]}
                drill=${() => changeRange(resolution === "month" ? "month" : "day", bucket.start.slice(0, 10))}/>
        </section>` : null}
        ${day ? html`<${DaySlots} buckets=${buckets} solar=${payload.solar_accuracy}/>` : null}
        <${Axle} axle=${payload.axle}/>
        <${Accuracy} solar=${payload.solar_accuracy} load=${payload.load_accuracy}
            meter=${payload.meter_reconciliation}/>`;
}

export function Statistics({repair}) {
    const [query, setQuery] = useState({kind: "day", at: null});
    const [result, setResult] = useState(null);
    const [failed, setFailed] = useState(null);
    const [retry, setRetry] = useState(0);
    const changeRange = (kind, at = null) => setQuery({kind, at});
    useEffect(() => {
        if (repair) return;
        const controller = new AbortController();
        const params = new URLSearchParams({range: query.kind});
        if (query.at) params.set("at", query.at);
        async function load() {
            try {
                const response = await fetch(`api/history?${params}`, {
                    cache: "no-store", signal: controller.signal,
                });
                if (!response.ok) throw new Error("statistics unavailable");
                const payload = await response.json();
                if (!controller.signal.aborted) setResult({query, retry, payload});
            } catch {
                if (!controller.signal.aborted) setFailed({query, retry});
            }
        }
        void load();
        return () => controller.abort();
    }, [repair, query, retry]);
    if (repair) return html`<section><h1>Statistics</h1><p>Configuration needs repair. Import a valid
        configuration with the CLI: <code>ha-spark config import FILE</code>.</p></section>`;
    const payload = result?.query === query && result.retry === retry ? result.payload : null;
    const range = payload?.range;
    const next = range && range.kind !== "all" ? stepDate(range, 1) : null;
    return html`<section>
        <h1>Statistics</h1>
        <div class="stats-ranges" role="group" aria-label="Statistics range">
            ${Object.entries(ranges).map(([kind, label]) => html`<button key=${kind}
                aria-pressed=${query.kind === kind} onClick=${() => changeRange(kind)}>${label}</button>`)}
        </div>
        ${query.kind === "all" ? null : html`<div role="group" aria-label="Step through periods">
            <button disabled=${!range || range.from.slice(0, 4) === "0001"}
                onClick=${() => changeRange(query.kind, stepDate(range, -1))}>Previous</button>
            <button disabled=${!next || next > range.today}
                onClick=${() => changeRange(query.kind, next)}>Next</button>
        </div>`}
        ${payload ? html`<${Results} key=${`${query.kind}:${query.at}:${retry}`}
            payload=${payload} changeRange=${changeRange}/>`
            : failed?.query === query && failed.retry === retry ? html`<p role="status">
                Statistics unavailable. <button onClick=${() => setRetry(retry + 1)}>Try again</button></p>`
                : html`<p role="status">Loading statistics…</p>`}
    </section>`;
}
