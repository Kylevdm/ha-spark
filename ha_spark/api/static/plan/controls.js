import {html, useState} from "../vendor/htm-3.1.1-preact-standalone.module.js";

// The Replan control: asks for a fresh plan through the CLI's replan path.
// The daemon alone decides and writes; nothing here plans anything.
export function Controls({health, reload}) {
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const repair = health.configuration?.status === "configuration_repair";
    const disabled = busy || health.daemon !== "running" || repair;
    async function replan() {
        if (disabled) return;
        setBusy(true);
        setMessage("Waiting for the daemon…");
        try {
            const response = await fetch("api/replan", {
                cache: "no-store",
                method: "POST",
                headers: {"Content-Type": "application/json", "X-HA-Spark-Request": "replan"},
                body: JSON.stringify({})
            });
            if (response.status === 504) {
                setMessage("still queued, reload rather than press again");
            } else if (!response.ok) {
                let error = "replan failed";
                try {
                    error = (await response.json()).error || error;
                } catch {}
                setMessage("Refused: " + error);
            } else {
                setMessage("");
                await reload();
            }
        } catch {
            setMessage("Request outcome unknown. Reload before deciding whether to try again.");
        } finally {
            setBusy(false);
        }
    }
    return html`<div class="plan-controls">
        <button type="button" onClick=${replan} disabled=${disabled}>Replan</button>
        <span role="status" aria-live="polite">${message}</span>
    </div>`;
}
