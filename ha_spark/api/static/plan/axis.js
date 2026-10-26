import {html, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {ActionLanes, SolarLane} from "./lanes.js";
import {bandClass} from "./carbon.js";
import {SocLane} from "./soc.js";

// The API has already resolved the household clock, including repeated hours.
export function clock(instant) {
    return instant.slice(0, 10) + " " + instant.slice(11, 16) + " " + instant.slice(19);
}

export function number(value, digits = 2) {
    return value == null ? "—" : Number(value).toFixed(digits);
}

export function Axis({timeline, selectedLine, history, carbon}) {
    const [selected, setSelected] = useState(null);
    const slots = timeline.slots;
    if (!slots?.length) return null;
    const horizon = Date.parse(timeline.horizon_start);
    const today = Date.parse(timeline.today_start);
    const start = today < horizon ? today : horizon;
    const duration = timeline.slot_minutes * 60000;
    const end = Date.parse(slots[slots.length - 1].start) + duration;
    const width = Math.max(960, (end - start) / duration * 24 + 140);
    const left = 130;
    const right = width - 20;
    const xMs = ms => left + (ms - start) / (end - start) * (right - left);
    const x = at => xMs(Date.parse(at));
    const slotWidth = duration / (end - start) * (right - left);
    const cells = [];
    for (let at = start; at < end; at += duration) cells.push(at);
    const labels = new Map([...(history?.buckets ?? []), ...slots]
        .map(slot => [Date.parse(slot.start), slot.start]));
    const rows = new Map((carbon?.rows ?? []).map(row => [Date.parse(row.start), row]));
    const endsAt = carbon?.ends_at ? Date.parse(carbon.ends_at) : null;
    const detail = selected == null ? null : slots[selected];
    const soc = detail ? timeline.soc?.find(point =>
        Date.parse(point.at) === Date.parse(detail.start))?.soc_pct : null;
    return html`<section aria-label="Plan timeline">
        <p>Household clock · select a half-hour for its numbers</p>
        <div class="chart-scroll" tabindex="0" aria-label="Scrollable plan chart">
            <svg viewBox=${`0 0 ${width} 730`} width=${width} height="730"
                 role="group" aria-label="Half-hour plan forecast">
                ${cells.map((at, i) => {
                    const label = labels.get(at);
                    return html`<g key=${at}>
                        <line class="slot-grid" x1=${xMs(at)} x2=${xMs(at)} y1="35" y2="675"/>
                        ${i % 6 === 0 && label ? html`<text class="axis-label" x=${xMs(at)} y="695">
                            <tspan x=${xMs(at)}>${label.slice(11, 16)} ${label.slice(19)}</tspan>
                            <tspan x=${xMs(at)} dy="16">${label.slice(0, 10)}</tspan>
                        </text>` : null}
                    </g>`;
                })}
                ${selectedLine ? html`<rect class="line-span"
                    x=${Math.max(xMs(horizon), x(selectedLine.start))} y="35"
                    width=${Math.max(0, Math.min(right, x(selectedLine.end))
                        - Math.max(xMs(horizon), x(selectedLine.start)))} height="640"/>` : null}
                <${SocLane} timeline=${timeline} x=${x} left=${xMs(horizon)} right=${right}/>
                <${SolarLane} timeline=${timeline} history=${history} x=${x}
                    slotWidth=${slotWidth} left=${left} right=${right}/>
                <g aria-label="Grid carbon intensity by half-hour">
                    <text class="lane-label" x="4" y="285">Carbon</text>
                    ${cells.map(at => {
                        const row = rows.get(at);
                        if (!row) return null;
                        return html`<rect key=${at}
                            class=${`carbon-cell ${bandClass(row.index)} ${row.estimated ? "carbon-estimated" : ""}`}
                            x=${xMs(at)} y="270" width=${slotWidth} height="40">
                            <title>${clock(row.start)}–${clock(row.end)} ·
                                ${number(row.gco2, 0)} gCO2/kWh · ${row.index}
                                ${row.estimated ? " · estimated" : ""}</title>
                        </rect>`;
                    })}
                    ${endsAt != null && endsAt >= start && endsAt < end ? html`<g>
                        <line class="carbon-end" x1=${xMs(endsAt)} x2=${xMs(endsAt)} y1="264" y2="316"/>
                        <text class="carbon-end-label" x=${xMs(endsAt)} y="258">
                            carbon forecast ends here</text>
                    </g>` : null}
                </g>
                <${ActionLanes} timeline=${timeline} x=${x} slotWidth=${slotWidth}
                    left=${left} right=${right}/>
                <line class="now-marker" x1=${x(timeline.now)} x2=${x(timeline.now)} y1="24" y2="675"/>
                <text class="now-label" x=${x(timeline.now)} y="18">now</text>
                ${slots.map((slot, i) => html`<rect key=${slot.start}
                    class=${selected === i ? "slot-hit selected" : "slot-hit"}
                    x=${x(slot.start)} y="35" width=${slotWidth} height="640"
                    tabindex="0" role="button" aria-label=${clock(slot.start)}
                    aria-pressed=${selected === i}
                    onClick=${() => setSelected(i)}
                    onKeyDown=${event => {
                        if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setSelected(i);
                        }
                    }}><title>${clock(slot.start)}</title></rect>`)}
            </svg>
        </div>
        ${detail ? html`<section class="slot-details" aria-live="polite">
            <h2><time datetime=${detail.start}>${clock(detail.start)}</time></h2>
            <p>Slot totals cover only the time still to come.</p>
            <dl>
                <div><dt>Import price</dt><dd>£ ${number(detail.import_price, 4)}/kWh</dd></div>
                <div><dt>Export price</dt><dd>£ ${number(detail.export_price, 4)}/kWh</dd></div>
                <div><dt>Cheap fraction</dt><dd>${number(detail.cheap_frac, 3)}</dd></div>
                <div><dt>Solar forecast</dt><dd>${number(detail.solar_raw_kwh)} kWh</dd></div>
                <div><dt>Solar after haircut</dt><dd>${number(detail.solar_kwh)} kWh</dd></div>
                <div><dt>SoC forecast</dt><dd>${soc == null ? "—" : `${number(soc, 1)}%`}</dd></div>
                <div><dt>Load forecast</dt><dd>${number(detail.load_kwh)} kWh</dd></div>
                <div><dt>Actions</dt><dd>${detail.actions.join(", ") || "—"}</dd></div>
            </dl>
        </section>` : null}
    </section>`;
}
