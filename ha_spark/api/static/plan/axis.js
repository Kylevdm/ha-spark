import {html, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {ActionLanes} from "./lanes.js";
import {SocLane} from "./soc.js";

// The API has already resolved the household clock, including repeated hours.
export function clock(instant) {
    return instant.slice(0, 10) + " " + instant.slice(11, 16) + " " + instant.slice(19);
}

export function number(value, digits = 2) {
    return value == null ? "—" : Number(value).toFixed(digits);
}

export function Axis({timeline}) {
    const [selected, setSelected] = useState(null);
    const slots = timeline.slots;
    if (!slots?.length) return null;
    const width = Math.max(960, slots.length * 24 + 140);
    const left = 130;
    const right = width - 20;
    const start = Date.parse(slots[0].start);
    const duration = timeline.slot_minutes * 60000;
    const end = Date.parse(slots[slots.length - 1].start) + duration;
    const x = at => left + (Date.parse(at) - start) / (end - start) * (right - left);
    const slotWidth = duration / (end - start) * (right - left);
    const detail = selected == null ? null : slots[selected];
    return html`<section aria-label="Plan timeline">
        <p>Household clock · select a half-hour for its numbers</p>
        <div class="chart-scroll" tabindex="0" aria-label="Scrollable plan chart">
            <svg viewBox=${`0 0 ${width} 630`} width=${width} height="630"
                 role="group" aria-label="Half-hour plan forecast">
                ${slots.map((slot, i) => html`<g key=${slot.start}>
                    <line class="slot-grid" x1=${x(slot.start)} x2=${x(slot.start)} y1="35" y2="575"/>
                    ${i % 6 === 0 ? html`<text class="axis-label" x=${x(slot.start)} y="595">
                        <tspan x=${x(slot.start)}>${slot.start.slice(11, 16)} ${slot.start.slice(19)}</tspan>
                        <tspan x=${x(slot.start)} dy="16">${slot.start.slice(0, 10)}</tspan>
                    </text>` : null}
                </g>`)}
                <${SocLane} timeline=${timeline} x=${x} left=${left} right=${right}/>
                <g transform="translate(0 100)">
                    <${ActionLanes} timeline=${timeline} x=${x} slotWidth=${slotWidth}
                        left=${left} right=${right}/>
                </g>
                <line class="now-marker" x1=${x(timeline.now)} x2=${x(timeline.now)} y1="24" y2="575"/>
                <text class="now-label" x=${x(timeline.now)} y="18">now</text>
                ${slots.map((slot, i) => html`<rect key=${slot.start}
                    class=${selected === i ? "slot-hit selected" : "slot-hit"}
                    x=${x(slot.start)} y="35" width=${slotWidth} height="540"
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
                <div><dt>Solar forecast</dt><dd>${number(detail.solar_kwh)} kWh</dd></div>
                <div><dt>Load forecast</dt><dd>${number(detail.load_kwh)} kWh</dd></div>
                <div><dt>Actions</dt><dd>${detail.actions.join(", ") || "—"}</dd></div>
            </dl>
        </section>` : null}
    </section>`;
}
