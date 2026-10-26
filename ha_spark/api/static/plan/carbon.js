// Display-only NESO carbon strip and region picker. The UI only renders; all
// banding and wording come from /api/carbon.
import {html, useEffect, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {clock, number} from "./axis.js";

const REGIONS = [
    "North Scotland", "South Scotland", "North West England", "North East England",
    "Yorkshire", "North Wales & Merseyside", "South Wales", "West Midlands",
    "East Midlands", "East England", "South West England", "South England", "London",
    "South East England", "England", "Scotland", "Wales",
];
const SLOT_MS = 30 * 60 * 1000;
const LABELS = ["very low", "low", "moderate", "high", "very high"];

function bandClass(index) {
    return `carbon-${index.replace(/ /g, "-")}`;
}

function stripBounds(rows, timeline) {
    if (!timeline?.slots?.length) return null;
    const lastSlot = timeline.slots[timeline.slots.length - 1];
    const lastEnd = Date.parse(lastSlot.start) + SLOT_MS;
    const firstRow = rows[0]?.start ? Date.parse(rows[0].start) : Date.parse(timeline.horizon_start);
    const firstStart = Math.min(firstRow, Date.parse(timeline.horizon_start));
    return {firstStart, lastEnd};
}

export function CarbonStrip({health, timeline}) {
    const [config, setConfig] = useState(null);
    const [open, setOpen] = useState(false);
    const [choice, setChoice] = useState("");
    const [busy, setBusy] = useState(false);
    const [conflict, setConflict] = useState(false);
    const [message, setMessage] = useState("");
    const [carbon, setCarbon] = useState(null);
    const available = health.daemon === "running"
        && health.configuration?.status !== "configuration_repair";
    useEffect(() => {
        if (!available) return;
        let active = true;
        async function load() {
            try {
                const response = await fetch("api/config", {cache: "no-store"});
                if (!response.ok) throw new Error();
                const value = await response.json();
                if (active) {
                    setConfig(value);
                    setChoice(value.settings.carbon_region ?? "");
                }
            } catch {
                if (active) setMessage("Carbon settings unavailable. Reload to try again.");
            }
        }
        void load();
        return () => { active = false; };
    }, [available]);
    useEffect(() => {
        if (!available || !config) return;
        let active = true;
        setCarbon(null);
        async function load() {
            try {
                const response = await fetch("api/carbon", {cache: "no-store"});
                if (!response.ok) throw new Error();
                const value = await response.json();
                if (active) setCarbon(value);
            } catch {
                if (active) setCarbon({status: "unavailable", rows: [], attribution: ""});
            }
        }
        void load();
        return () => { active = false; };
    }, [available, config?.settings.carbon_region, timeline?.now]);
    async function save() {
        if (!config || busy || conflict || !available) return;
        setBusy(true);
        setMessage("");
        try {
            const region = choice === "" ? null : Number(choice);
            const response = await fetch("api/config", {
                method: "POST",
                headers: {"X-HA-Spark-Request": "config", "Content-Type": "application/json"},
                body: JSON.stringify({
                    changes: {carbon_region: region}, expected_revision: config.revision,
                }),
            });
            if (response.status === 409) {
                setConflict(true);
                setMessage("Carbon region changed elsewhere; reload before saving again.");
                return;
            }
            if (!response.ok) throw new Error();
            const result = await response.json();
            if (result.status === "pending_restart") {
                setConflict(true);
                setMessage("Configuration saved; restart required before it becomes live.");
                return;
            }
            setConfig({...config, revision: result.revision,
                settings: {...config.settings, carbon_region: region}});
            setOpen(false);
            setMessage("Carbon region saved.");
        } catch {
            setConflict(true);
            setMessage("Could not confirm the save. Reload before trying again.");
        } finally {
            setBusy(false);
        }
    }
    if (!available) return null;
    const region = config?.settings.carbon_region;
    const picker = html`${config ? html`<a href="#carbon-region" onClick=${event => {
            event.preventDefault(); setOpen(true);
        }}>${region == null ? "Pick a region to show grid carbon"
            : `Grid carbon region: ${REGIONS[region - 1]} — change region`}</a>` : null}
        ${open ? html`<div id="carbon-region">
            <label>Grid carbon region
                <select value=${choice} disabled=${busy || conflict}
                    onChange=${event => setChoice(event.currentTarget.value)}>
                    <option value="">Turn off grid carbon</option>
                    ${REGIONS.map((name, index) => html`
                        <option value=${index + 1}>${name}</option>`)}
                </select>
            </label>
            <button onClick=${save} disabled=${busy || conflict}>Save region</button>
            <button onClick=${() => setOpen(false)} disabled=${busy}>Cancel</button>
        </div>` : null}`;
    if (carbon?.status === "off" || carbon == null) {
        return html`<section aria-label="Grid carbon">${picker}
            <p role="status">${message}</p>
        </section>`;
    }
    if (carbon.status === "unavailable") {
        return html`<section aria-label="Grid carbon">${picker}
            <p role="status">Grid carbon unavailable.</p>
            <p role="status">${message}</p>
        </section>`;
    }
    const bounds = stripBounds(carbon.rows, timeline);
    const note = carbon.status === "stale"
        ? html`<p role="status">Carbon data may be out of date.</p>` : null;
    const legend = html`<div class="carbon-legend">
        ${LABELS.map(label => html`<span key=${label} class="carbon-swatch ${bandClass(label)}">
            ${label}</span>`)}
        <span class="carbon-attribution">${carbon.attribution}</span>
    </div>`;
    if (!bounds) {
        return html`<section aria-label="Grid carbon">${picker}${note}${legend}
            <p role="status">${message}</p>
        </section>`;
    }
    const {firstStart, lastEnd} = bounds;
    const duration = lastEnd - firstStart;
    const width = Math.max(960, Math.ceil(duration / SLOT_MS) * 24 + 140);
    const left = 130;
    const right = width - 20;
    const xMs = ms => left + (ms - firstStart) / duration * (right - left);
    const x = at => xMs(Date.parse(at));
    const cellWidth = SLOT_MS / duration * (right - left);
    const rowsByStart = new Map(carbon.rows.map(row => [Date.parse(row.start), row]));
    const cells = [];
    for (let t = firstStart; t < lastEnd; t += SLOT_MS) {
        cells.push(t);
    }
    const nowX = x(timeline.now);
    const endsAt = carbon.ends_at ? Date.parse(carbon.ends_at) : null;
    return html`<section aria-label="Grid carbon">
        ${picker}${note}
        <div class="chart-scroll" tabindex="0" aria-label="Scrollable carbon intensity strip">
            <svg viewBox=${`0 0 ${width} 90`} width=${width} height="90"
                 role="group" aria-label="Grid carbon intensity by half-hour">
                <text class="lane-label" x="4" y="20">Carbon</text>
                ${cells.map(t => {
                    const row = rowsByStart.get(t);
                    if (!row) return null;
                    const estimated = row.estimated;
                    const x0 = x(row.start);
                    return html`<rect key=${t}
                        class=${`carbon-cell ${bandClass(row.index)} ${estimated ? "carbon-estimated" : ""}`}
                        x=${x0} y="24" width=${cellWidth} height="40">
                        <title>${clock(row.start)}–${clock(row.end)} ·
                            ${number(row.gco2, 0)} gCO2/kWh · ${row.index}
                            ${estimated ? " · estimated" : ""}</title>
                    </rect>`;
                })}
                ${endsAt != null && endsAt < lastEnd ? html`<g>
                    <line class="carbon-end" x1=${xMs(endsAt)} x2=${xMs(endsAt)} y1="18" y2="70"/>
                    <text class="carbon-end-label" x=${xMs(endsAt)} y="14">
                        carbon forecast ends here</text>
                </g>` : null}
                <line class="now-marker" x1=${nowX} x2=${nowX} y1="18" y2="70"/>
                <text class="now-label" x=${nowX} y="14">now</text>
            </svg>
        </div>
        ${legend}
        <p role="status">${message}</p>
    </section>`;
}
