import {html} from "../vendor/htm-3.1.1-preact-standalone.module.js";

export function LinesList({timeline, selectedLine, selectLine}) {
    return html`<ul class="plan-lines">${timeline.lines.map((line, i) => html`
        <li key=${i} class=${line.kind === "warning" ? "plan-warnings" : null}>
            ${line.start && line.end ? html`<button type="button"
                aria-pressed=${selectedLine === line}
                onClick=${() => selectLine(selectedLine === line ? null : line)}>
                ${line.text}</button>` : line.text}
        </li>`)}</ul>`;
}
