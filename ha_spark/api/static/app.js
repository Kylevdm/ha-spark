import {html, render, useState, useEffect, useRef, useCallback}
    from "./vendor/htm-3.1.1-preact-standalone.module.js";
import {mountMode} from "./mode.js";

function screenFromHash() {
    return location.hash === "#/mode" ? "mode" : "plan";
}

function Mode({health, refresh}) {
    const root = useRef(null);
    const repair = health.configuration?.status;
    useEffect(() => mountMode(root.current, health, refresh), [health.daemon, repair]);
    return html`<div ref=${root}></div>`;
}

function Plan({health}) {
    if (health.configuration?.status === "configuration_repair") {
        return html`<section><h1>Plan</h1><p>Configuration needs repair. Import a valid
            configuration with the CLI: <code>ha-spark config import FILE</code>.</p></section>`;
    }
    if (health.daemon !== "running") {
        return html`<section><h1>Plan</h1><p>daemon not running</p></section>`;
    }
    return html`<section><h1>Plan</h1><p>no plan yet</p></section>`;
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
