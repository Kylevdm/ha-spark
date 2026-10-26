import {html, render} from "./vendor/htm-3.1.1-preact-standalone.module.js";

export function mountMode(root, health, refreshHealth) {
    render(html`<section>
        <h1>Site operating mode</h1>
        <p>Home Assistant administrators only. Settings and agents cannot change authority.</p>
        <div id="message" role="status" aria-live="polite">Loading…</div>
        <button id="refresh" type="button">Refresh status</button>
        <div id="status"></div>
        <section aria-label="Change operating mode" id="actions"></section>
        <dialog id="confirmation" aria-labelledby="confirm-title">
            <h2 id="confirm-title">Confirm operating mode</h2>
            <p id="confirm-text"></p>
            <button id="confirm" type="button">Confirm change</button>
            <button id="cancel" type="button">Cancel</button>
        </dialog>
    </section>`, root);
    const statusNode = root.querySelector("#status");
    const actions = root.querySelector("#actions");
    const message = root.querySelector("#message");
    const dialog = root.querySelector("#confirmation");
    const refreshButton = root.querySelector("#refresh");
    const readOnly = health.configuration?.status === "configuration_repair";
    const daemonRunning = health.daemon === "running";
    let displayed = null;
    let pending = null;
    let busy = false;
    let active = true;

    function element(tag, text, parent) {
        const node = document.createElement(tag);
        node.textContent = text;
        parent.appendChild(node);
        return node;
    }

    function detail(label, value) {
        element("h2", label, statusNode);
        element("pre", typeof value === "string" ? value : JSON.stringify(value, null, 2), statusNode);
    }

    function show(view) {
        displayed = view;
        statusNode.replaceChildren();
        actions.replaceChildren();
        detail("Mode", view.mode === "authority_unknown" ? "authority unknown" : view.mode);
        detail("Epoch", view.epoch);
        if (view.transition) {
            detail("Transition", view.transition.target === "observe"
                ? "switching to Observe" : "activating " + view.transition.target);
            detail("Transition details", view.transition);
        } else {
            detail("Transition", "none");
        }
        detail("Failing actions", view.failing_actions);
        detail("Holds and causes", view.holds);
        detail("Fallback and causes", view.fallback);
        detail("Last change", view.last_changed_at);
        detail("Requester kind", view.requester_kind);
        if (view.save_failed) detail("Authority fault", "Authority store save failed; writes are blocked.");
        if (readOnly || view.suspended === "configuration_repair") {
            detail("Suspended", "Configuration needs repair: protection and optimisation are "
                + "suspended until a valid configuration is imported.");
            return;
        }
        element("h2", "Entry preconditions (checked again when requested)", statusNode);
        for (const target of ["observe", "protect", "optimise"]) {
            const report = view.preconditions?.[target] || {passed: false, reasons: []};
            element("h3", target + ": " + (report.passed ? "passed" : "not met"), statusNode);
            const list = element("ul", "", statusNode);
            for (const reason of report.reasons) element("li", reason, list);
            if (view.mode === "authority_unknown" && target !== "observe") continue;
            const button = element("button", view.mode === "authority_unknown"
                ? "Confirm Observe" : "Request " + target, actions);
            button.type = "button";
            button.disabled = busy || !daemonRunning;
            button.addEventListener("click", () => choose(target));
        }
    }

    async function reload() {
        if (!active) return;
        displayed = null;
        actions.replaceChildren();
        if (!daemonRunning) {
            show(health.authority);
            message.textContent = "daemon not running";
            return;
        }
        try {
            const response = await fetch("api/mode", {cache: "no-store"});
            if (!response.ok) throw new Error("status unavailable");
            const body = await response.json();
            // A status without preconditions can't back a request; treat it as a failed read.
            if (!body.preconditions && !readOnly && body.suspended !== "configuration_repair") {
                throw new Error("status incomplete");
            }
            if (active) show(body);
        } catch {
            message.textContent = "Could not load current authority. Refresh before requesting a change.";
        }
    }

    function choose(target) {
        if (!active || busy || !displayed || !daemonRunning || readOnly) return;
        // Capture the epoch the owner saw, not a later refreshed value.
        const request = {target, expected_epoch: displayed.mode === "authority_unknown"
            ? null : displayed.epoch};
        if (target === "observe") {
            void submit(request);
            return;
        }
        pending = request;
        root.querySelector("#confirm-text").textContent = target === "optimise"
            ? "Enable Optimise? ha-spark will arm Axle export windows when configured. Export remains "
              + "supervised: a person must be present for preparation and verified cleanup. "
              + "Before enabling, disable any pre-existing automations or manual schedules that write "
              + "the same devices."
            : "Enable Protect? ha-spark will apply and release battery-to-car holds. Your charging "
              + "schedule remains yours; leaving Optimise first hands over the fallback charging schedule.";
        dialog.showModal();
    }

    async function submit(request) {
        busy = true;
        actions.replaceChildren();
        refreshButton.disabled = true;
        message.textContent = "Waiting for the daemon…";
        try {
            const response = await fetch("api/mode", {
                method: "POST",
                headers: {"Content-Type": "application/json", "X-HA-Spark-Request": "mode"},
                body: JSON.stringify(request)
            });
            const body = await response.json();
            if (response.status === 409) {
                message.textContent = "Conflict: " + body.error
                    + ". Status reloaded. Review it and click again; nothing is retried automatically.";
            } else if (response.status === 504) {
                message.textContent = "Request queued; the outcome is not yet known. Reloading status.";
            } else if (!response.ok) {
                message.textContent = "Refused: " + body.error;
                const list = element("ul", "", message);
                for (const reason of body.reasons || []) element("li", reason, list);
                if (body.detail) element("p", body.detail, message);
            } else {
                message.textContent = "Request accepted. Hardware state is shown by the transition and failures below.";
            }
        } catch {
            message.textContent = "Request outcome unknown. Reload status before deciding whether to try again.";
        } finally {
            busy = false;
            if (active) {
                refreshButton.disabled = false;
                await reload();
                void refreshHealth();
            }
        }
    }

    root.querySelector("#confirm").addEventListener("click", () => {
        const request = pending;
        pending = null;
        dialog.close();
        if (request && active && !busy) void submit(request);
    });
    root.querySelector("#cancel").addEventListener("click", () => {
        pending = null;
        dialog.close();
    });
    dialog.addEventListener("cancel", () => { pending = null; });
    refreshButton.addEventListener("click", () => {
        void reload();
        void refreshHealth();
    });
    void reload().then(() => {
        if (active && displayed && daemonRunning) {
            message.textContent = "Review status before requesting a change.";
        }
    });
    return () => {
        active = false;
        pending = null;
        dialog.close();
        render(null, root);
    };
}
