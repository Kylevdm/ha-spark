// Owner-only Settings screen. This phase holds one section: Statistics data.
// The server owns the claim and the counts; this view only reports and requests.
import {html, useEffect, useState} from "./vendor/htm-3.1.1-preact-standalone.module.js";

// The last run as one sentence; tolerant of a missing or malformed timestamp.
export function repairSummary(run) {
    if (run == null) return "Never run.";
    if (run.status === "running") return "Running.";
    const when = run.started_at ? new Date(run.started_at) : null;
    const at = when && !Number.isNaN(when.getTime()) ? when.toLocaleString() : "unknown time";
    if (run.status === "failed") return `Last run ${at}: failed.`;
    return `Last run ${at}: checked ${run.checked_days ?? 0} days, ` +
        `fixed ${run.fixed_slots ?? 0} slots, ${run.still_gaps ?? 0} still gaps.`;
}

const POLL_MS = 10000;

export function SettingsScreen({repair}) {
    const [run, setRun] = useState(null);
    const [failed, setFailed] = useState(false);
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const [retry, setRetry] = useState(0);

    useEffect(() => {
        if (repair) return;
        const controller = new AbortController();
        let timer = null;
        async function load() {
            try {
                const response = await fetch("api/statistics/repair", {
                    cache: "no-store", signal: controller.signal,
                });
                if (!response.ok) throw new Error("repair status unavailable");
                const payload = await response.json();
                if (controller.signal.aborted) return;
                setRun(payload.last_run);
                setFailed(false);
                if (payload.last_run?.status === "running") {
                    timer = setTimeout(() => setRetry(retry + 1), POLL_MS);
                }
            } catch {
                if (!controller.signal.aborted) setFailed(true);
            }
        }
        void load();
        return () => {
            controller.abort();
            if (timer) clearTimeout(timer);
        };
    }, [repair, retry]);

    async function startRepair() {
        if (busy || run?.status === "running") return;
        setBusy(true);
        setMessage("Starting…");
        try {
            const response = await fetch("api/statistics/repair", {
                method: "POST",
                headers: {"Content-Type": "application/json", "X-HA-Spark-Request": "repair_stats"},
            });
            if (response.status === 409) {
                setMessage("A repair is already running; this page follows its progress.");
            } else if (response.status === 202) {
                setMessage("Repair started. It runs in the background; this section updates as it goes.");
            } else {
                setMessage("Repair request refused. Reload and try again.");
            }
        } catch {
            setMessage("Repair request outcome unknown. Reload before deciding whether to try again.");
        } finally {
            setBusy(false);
            setRetry(retry + 1);
        }
    }

    if (repair) return html`<section><h1>Settings</h1><p>Configuration needs repair. Import a valid
        configuration with the CLI: <code>ha-spark config import FILE</code>.</p></section>`;
    return html`<section>
        <h1>Settings</h1>
        <h2>Statistics data</h2>
        <p role="status" aria-live="polite">${message}</p>
        <p>${failed ? "Last repair: unavailable." : repairSummary(run)}</p>
        <button type="button" disabled=${busy || run?.status === "running"}
            onClick=${startRepair}>Repair data</button>
        <p>Re-checks all stored energy-ledger history against the plausibility rule, clears
            rejected slots and gaps, and re-fills them from Home Assistant statistics
            (five-minute where retained, hourly otherwise). Values that pass are never touched;
            one run at a time; it writes only the stored statistics, never hardware. The CLI
            equivalent is <code>ha-spark repair-stats</code>.</p>
    </section>`;
}
