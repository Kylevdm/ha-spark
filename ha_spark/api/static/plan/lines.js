import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";

export function LinesList({timeline}) {
    return html`<ul class="plan-warnings">${timeline.lines.map((line, i) => html`
        <li key=${i}>${line.text}</li>`)}</ul>`;
}
