import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";
import {Axis} from "./axis.js";
import {TodaySpan} from "./today.js";
import {LinesList} from "./lines.js";

// Each plan part owns its own rendering and requests. Controls and carbon are
// registered by their tickets; the SoC lane is drawn inside the shared axis.
const parts = [TodaySpan, Axis, LinesList];

export function PlanParts(props) {
    return html`${parts.map(Part => html`<${Part} ...${props}/>` )}`;
}
