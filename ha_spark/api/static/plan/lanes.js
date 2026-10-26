import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";

const actions = ["charge", "hold", "export", "skip", "v2l"];

// Step geometry only: no forecast interpolation or action inference in the UI.
// Slot 0 covers only the part still to come, so its step starts at "now".
function steps(slots, field, x, slotWidth, y, now) {
    return slots.map((slot, i) =>
        `${i ? "L" : "M"}${i ? x(slot.start) : Math.max(x(slot.start), x(now))},${y(slot[field])} ` +
        `L${x(slot.start) + slotWidth},${y(slot[field])}`).join(" ");
}

function Forecast({slots, fields, top, label, x, slotWidth, left, right, now}) {
    const values = slots.flatMap(slot => fields.map(field => slot[field]));
    const min = Math.min(0, ...values);
    const max = Math.max(0, ...values);
    const span = max - min || 1;
    const y = value => top + 70 - (value - min) / span * 60;
    return html`<g>
        <text class="lane-label" x="4" y=${top + 15}>${label}</text>
        <text class="axis-label" x="4" y=${top + 35}>${max.toFixed(3)}</text>
        <text class="axis-label" x="4" y=${top + 70}>${min.toFixed(3)}</text>
        <line class="lane-rule" x1=${left} x2=${right} y1=${y(0)} y2=${y(0)}/>
        ${fields.map(field => html`<path class=${`series ${field}`}
            d=${steps(slots, field, x, slotWidth, y, now)}/>`)}
    </g>`;
}

export function ActionLanes({timeline, x, slotWidth, left, right}) {
    const slots = timeline.slots;
    return html`<g>
        <${Forecast} slots=${slots} fields=${["solar_raw_kwh", "solar_kwh", "load_kwh"]} top=${40}
            label="Forecast kWh" x=${x} slotWidth=${slotWidth} left=${left} right=${right}
            now=${timeline.now}/>
        <text class="series-key solar_raw_kwh" x=${left} y="132">Solar forecast (dashed)</text>
        <text class="series-key solar_kwh" x=${left + 190} y="132">After haircut (dotted)</text>
        <text class="series-key load_kwh" x=${left + 380} y="132">House load</text>
        ${actions.map((action, row) => html`<g key=${action}>
            <text class="lane-label" x="4" y=${163 + row * 38}>${action}</text>
            <line class="lane-rule" x1=${left} x2=${right} y1=${174 + row * 38} y2=${174 + row * 38}/>
            ${slots.filter(slot => slot.actions.includes(action)).map(slot => {
                const lo = Math.max(x(slot.start), x(timeline.now));
                const hi = x(slot.start) + slotWidth;
                return html`<rect class=${`action ${action}`} x=${lo} y=${147 + row * 38}
                    width=${Math.max(0, hi - lo)} height="25"><title>${action}</title></rect>`;
            })}
        </g>`)}
        <${Forecast} slots=${slots} fields=${["import_price", "export_price"]} top=${365}
            label="Price £/kWh" x=${x} slotWidth=${slotWidth} left=${left} right=${right}
            now=${timeline.now}/>
        <text class="series-key import_price" x=${left} y="465">Import</text>
        <text class="series-key export_price" x=${left + 170} y="465">Export</text>
    </g>`;
}
