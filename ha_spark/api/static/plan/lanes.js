import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";

const actions = ["charge", "hold", "export", "skip", "v2l"];

// Step geometry only. Gaps break paths; horizon slot 0 starts at "now".
function steps(slots, field, x, slotWidth, y, now) {
    const path = [];
    let previousEnd = null;
    for (const slot of slots) {
        const value = slot[field];
        if (value == null) {
            previousEnd = null;
            continue;
        }
        const at = Date.parse(slot.start);
        const loAt = now ? Math.max(at, Date.parse(now)) : at;
        const lo = now ? Math.max(x(slot.start), x(now)) : x(slot.start);
        const hi = x(slot.start) + slotWidth;
        if (hi <= lo) continue;
        path.push(`${previousEnd === loAt ? "L" : "M"}${lo},${y(value)} L${hi},${y(value)}`);
        previousEnd = at + 30 * 60000;
    }
    return path.join(" ");
}

function Forecast({series, top, label, x, slotWidth, left, right}) {
    const values = series.flatMap(({slots, field}) => slots.map(slot => slot[field]))
        .filter(value => value != null);
    const min = Math.min(0, ...values);
    const max = Math.max(0, ...values);
    const span = max - min || 1;
    const y = value => top + 70 - (value - min) / span * 60;
    return html`<g>
        <text class="lane-label" x="4" y=${top + 15}>${label}</text>
        <text class="axis-label" x="4" y=${top + 35}>${max.toFixed(3)}</text>
        <text class="axis-label" x="4" y=${top + 70}>${min.toFixed(3)}</text>
        <line class="lane-rule" x1=${left} x2=${right} y1=${y(0)} y2=${y(0)}/>
        ${series.map(({slots, field, className, now}) => html`<path class=${`series ${className ?? field}`}
            d=${steps(slots, field, x, slotWidth, y, now)}/>`)}
    </g>`;
}

export function SolarLane({timeline, history, x, slotWidth, left, right}) {
    const slots = timeline.slots;
    const buckets = history?.buckets ?? [];
    const series = [
        {slots: buckets, field: "solar_forecast_kwh", className: "solar_raw_kwh"},
        {slots: buckets, field: "solar_measured_kwh"},
        ...["solar_raw_kwh", "solar_kwh", "load_kwh"].map(field =>
            ({slots, field, now: timeline.now})),
    ];
    return html`<g>
        <${Forecast} series=${series} top=${140} label="Solar / load kWh"
            x=${x} slotWidth=${slotWidth} left=${left} right=${right}/>
        <text class="series-key solar_raw_kwh" x=${left} y="232">Solar forecast (dashed)</text>
        <text class="series-key solar_kwh" x=${left + 190} y="232">After haircut (dotted)</text>
        <text class="series-key load_kwh" x=${left + 380} y="232">House load</text>
        <text class="series-key solar_measured_kwh" x=${left + 490} y="232">Measured (solid)</text>
    </g>`;
}

export function ActionLanes({timeline, x, slotWidth, left, right}) {
    const slots = timeline.slots;
    const series = ["import_price", "export_price"].map(field =>
        ({slots, field, now: timeline.now}));
    return html`<g>
        ${actions.map((action, row) => html`<g key=${action}>
            <text class="lane-label" x="4" y=${366 + row * 38}>${action}</text>
            <line class="lane-rule" x1=${left} x2=${right} y1=${377 + row * 38} y2=${377 + row * 38}/>
            ${slots.filter(slot => slot.actions.includes(action)).map(slot => {
                const lo = Math.max(x(slot.start), x(timeline.now));
                const hi = x(slot.start) + slotWidth;
                return html`<rect class=${`action ${action}`} x=${lo} y=${350 + row * 38}
                    width=${Math.max(0, hi - lo)} height="25"><title>${action}</title></rect>`;
            })}
        </g>`)}
        <${Forecast} series=${series} top=${565} label="Price £/kWh"
            x=${x} slotWidth=${slotWidth} left=${left} right=${right}/>
        <text class="series-key import_price" x=${left} y="665">Import</text>
        <text class="series-key export_price" x=${left + 170} y="665">Export</text>
    </g>`;
}
