// Display labels from NESO; the region is a display-only setting.
import {html, useEffect, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";

const REGIONS = [
    "North Scotland", "South Scotland", "North West England", "North East England",
    "Yorkshire", "North Wales & Merseyside", "South Wales", "West Midlands",
    "East Midlands", "East England", "South West England", "South England", "London",
    "South East England", "England", "Scotland", "Wales",
];

export function CarbonStrip({health, timeline}) {
    const [config, setConfig] = useState(null);
    const [open, setOpen] = useState(false);
    const [choice, setChoice] = useState("");
    const [busy, setBusy] = useState(false);
    const [conflict, setConflict] = useState(false);
    const [message, setMessage] = useState("");
    const available = health.daemon === "running"
        && health.configuration?.status !== "configuration_repair";
    useEffect(() => {
        if (!available) return;
        let active = true;
        async function load() {
            try {
                const response = await fetch("api/config", {cache: "no-store"});
                if (!response.ok) throw new Error();
                const value = await response.json();
                if (active) {
                    setConfig(value);
                    setChoice(value.settings.carbon_region ?? "");
                }
            } catch {
                if (active) setMessage("Carbon settings unavailable. Reload to try again.");
            }
        }
        void load();
        return () => { active = false; };
    }, [available]);
    async function save() {
        if (!config || busy || conflict || !available) return;
        setBusy(true);
        setMessage("");
        try {
            const region = choice === "" ? null : Number(choice);
            const response = await fetch("api/config", {
                method: "POST",
                headers: {"X-HA-Spark-Request": "config", "Content-Type": "application/json"},
                body: JSON.stringify({
                    changes: {carbon_region: region}, expected_revision: config.revision,
                }),
            });
            if (response.status === 409) {
                setConflict(true);
                setMessage("Carbon region changed elsewhere; reload before saving again.");
                return;
            }
            if (!response.ok) throw new Error();
            const result = await response.json();
            if (result.status === "pending_restart") {
                setConflict(true);
                setMessage("Configuration saved; restart required before it becomes live.");
                return;
            }
            setConfig({...config, revision: result.revision,
                settings: {...config.settings, carbon_region: region}});
            setOpen(false);
            setMessage("Carbon region saved.");
        } catch {
            setConflict(true);
            setMessage("Could not confirm the save. Reload before trying again.");
        } finally {
            setBusy(false);
        }
    }
    if (!available) return null;
    const region = config?.settings.carbon_region;
    return html`<section aria-label="Grid carbon">
        ${config ? html`<a href="#carbon-region" onClick=${event => {
            event.preventDefault(); setOpen(true);
        }}>${region == null ? "Pick a region to show grid carbon"
            : `Grid carbon region: ${REGIONS[region - 1]} — change region`}</a>` : null}
        ${open ? html`<div id="carbon-region">
            <label>Grid carbon region
                <select value=${choice} disabled=${busy || conflict}
                    onChange=${event => setChoice(event.currentTarget.value)}>
                    <option value="">Turn off grid carbon</option>
                    ${REGIONS.map((name, index) => html`
                        <option value=${index + 1}>${name}</option>`)}
                </select>
            </label>
            <button onClick=${save} disabled=${busy || conflict}>Save region</button>
            <button onClick=${() => setOpen(false)} disabled=${busy}>Cancel</button>
        </div>` : null}
        <p role="status">${message}</p>
    </section>`;
}
