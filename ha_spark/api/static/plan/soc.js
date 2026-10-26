import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";

// Join only the planner's points. No additional forecast values are created here.
export function SocLane({timeline, x, left, right}) {
    const points = timeline.soc;
    const {min_soc, target_soc} = timeline.headline;
    const y = value => 120 - value * 0.8;
    return html`<g aria-label="Battery SoC forecast">
        <text class="lane-label" x="4" y="49">SoC forecast %</text>
        <text class="axis-label" x="4" y="70">100</text>
        <text class="axis-label" x="4" y="120">0</text>
        <line class="lane-rule" x1=${left} x2=${right} y1="120" y2="120"/>
        <line class="soc-min" x1=${left} x2=${right} y1=${y(min_soc)} y2=${y(min_soc)}>
            <title>Minimum SoC ${min_soc}%</title>
        </line>
        <text class="axis-label" x=${right} y=${y(min_soc) - 3} text-anchor="end">
            Minimum ${min_soc}%
        </text>
        ${target_soc == null ? null : html`<g>
            <line class="soc-target" x1=${left} x2=${right}
                y1=${y(target_soc)} y2=${y(target_soc)}/>
            <text class="axis-label" x=${right} y=${y(target_soc) - 3} text-anchor="end">
                Target ${target_soc.toFixed(1)}%
            </text>
        </g>`}
        ${points?.length ? html`<polyline class="series soc-forecast"
            points=${points.map(point => `${x(point.at)},${y(point.soc_pct)}`).join(" ")}/>` : null}
        ${points?.map(point => html`<circle class="soc-point" key=${point.at}
            cx=${x(point.at)} cy=${y(point.soc_pct)} r="2">
            <title>${point.at} · ${point.soc_pct.toFixed(1)}% · forecast</title>
        </circle>`)}
    </g>`;
}
