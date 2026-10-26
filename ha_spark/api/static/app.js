import {html, render, useState, useEffect, useRef, useCallback}
    from "./vendor/htm-3.1.1-preact-standalone.module.js";
import {mountMode} from "./mode.js";
import {CarbonStrip} from "./plan/carbon.js";

function screenFromHash() {
    return location.hash === "#/mode" ? "mode" : "plan";
}

function Mode({health, refresh}) {
    const root = useRef(null);
    const repair = health.configuration?.status;
    useEffect(() => mountMode(root.current, health, refresh), [health.daemon, repair]);
    return html`<div ref=${root}></div>`;
}

// Display rounding only; the values themselves come from the timeline.
function fmt(value, digits) {
    return value == null ? "—" : Number(value).toFixed(digits);
}

function Plan({health}) {
    const [payload, setPayload] = useState(null);
    const [failed, setFailed] = useState(false);
    const repair = health.configuration?.status === "configuration_repair";
    useEffect(() => {
        if (repair || health.daemon !== "running") return;
        let active = true;
        setPayload(null);
        setFailed(false);
        async function load() {
            try {
                const response = await fetch("api/plan", {cache: "no-store"});
                if (!response.ok) throw new Error("plan unavailable");
                const result = await response.json();
                if (active) setPayload(result);
            } catch {
                if (active) setFailed(true);
            }
        }
        void load();
        return () => { active = false; };
    }, [repair, health.daemon, health.plan_at]);
    if (repair) {
        return html`<section><h1>Plan</h1><p>Configuration needs repair. Import a valid
            configuration with the CLI: <code>ha-spark config import FILE</code>.</p></section>`;
    }
    if (health.daemon !== "running") {
        return html`<section><h1>Plan</h1><p>daemon not running</p></section>`;
    }
    if (failed) return html`<section><h1>Plan</h1><p>Plan unavailable. Reload to try again.</p></section>`;
    if (!payload) return html`<section><h1>Plan</h1><p>Loading plan…</p></section>`;
    const timeline = payload.timeline;
    if (!timeline) return html`<section><h1>Plan</h1><p>no plan yet</p></section>`;
    const {headline, costs} = timeline;
    return html`<section>
        <h1>Plan</h1>
        <p>Computed at <time datetime=${payload.generated_at}>${payload.generated_at}</time></p>
        <p>Model: ${timeline.model} · Strategy: ${timeline.strategy}</p>
        <dl class="plan-headline">
            <div><dt>Battery SoC</dt><dd>${fmt(headline.soc_now, 0)} %</dd></div>
            <div><dt>Target SoC</dt><dd>${fmt(headline.target_soc, 0)} %</dd></div>
            <div><dt>Charge needed</dt><dd>${fmt(headline.required_kwh, 2)} kWh</dd></div>
            <div><dt>Minimum SoC</dt><dd>${fmt(headline.min_soc, 0)} %</dd></div>
            <div><dt>Battery capacity</dt><dd>${fmt(headline.capacity_kwh, 1)} kWh</dd></div>
        </dl>
        <dl class="plan-costs">
            <div><dt>Planned cost</dt><dd>£ ${fmt(costs.planned, 2)}</dd></div>
            <div><dt>Without battery</dt><dd>£ ${fmt(costs.baseline, 2)}</dd></div>
            ${costs.export_revenue != null && costs.export_revenue !== 0 ? html`
                <div><dt>Export revenue</dt><dd>£ ${fmt(costs.export_revenue, 2)}</dd></div>` : null}
        </dl>
        <${CarbonStrip} health=${health} timeline=${timeline}/>
        <ul class="plan-warnings">${timeline.lines.map(line => html`
            <li>${line.text}</li>`)}</ul>
    </section>`;
}

function App() {
    const [screen, setScreen] = useState(screenFromHash);
    const [health, setHealth] = useState(null);
    const [failed, setFailed] = useState(false);
    const refresh = useCallback(async () => {
        try {
            const response = await fetch("api/health", {cache: "no-store"});
            if (!response.ok) throw new Error("health unavailable");
            setHealth(await response.json());
            setFailed(false);
        } catch {
            // Never leave stale authority controls available after a failed status read.
            setFailed(true);
        }
    }, []);
    useEffect(() => {
        if (!location.hash || !["#/plan", "#/mode"].includes(location.hash)) {
            location.replace("#/plan");
        }
        const navigate = () => { setScreen(screenFromHash()); void refresh(); };
        addEventListener("hashchange", navigate);
        void refresh();
        return () => removeEventListener("hashchange", navigate);
    }, []);
    const authority = health?.authority;
    return html`<div class="shell">
        <nav aria-label="Main navigation">
            <strong>ha-spark</strong>
            <a href="#/plan" aria-current=${screen === "plan" ? "page" : null}>Plan</a>
            <a href="#/mode" aria-current=${screen === "mode" ? "page" : null}>Mode</a>
        </nav>
        <div class="workspace">
            <header aria-live="polite">
                ${failed ? html`<p>Could not load current authority. Refresh before requesting a change.
                    <button onClick=${refresh}>Refresh status</button></p>` : health ? html`
                    <p>Operating mode: <strong>${health.operating_mode}</strong></p>
                    <p>Authority: ${health.operating_mode === "authority_unknown" ? "unknown" : "known"}
                        ${authority.save_failed ? " — save_failed; writes are blocked" : ""}</p>
                    <p>Fallback: ${JSON.stringify(authority.fallback)}</p>
                    ${health.configuration?.status === "configuration_repair"
                        ? html`<p>Stored authority suspended — configuration repair</p>` : null}
                    ${health.daemon === "unavailable" ? html`<p>daemon not running</p>` : null}
                ` : html`<p>Loading status…</p>`}
            </header>
            <main>${!failed && health ? screen === "mode"
                ? html`<${Mode} health=${health} refresh=${refresh}/>`
                : html`<${Plan} health=${health}/>` : null}</main>
        </div>
    </div>`;
}

render(html`<${App}/>`, document.getElementById("app"));
