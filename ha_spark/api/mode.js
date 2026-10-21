"use strict";

const statusNode = document.getElementById("status");
const actions = document.getElementById("actions");
const message = document.getElementById("message");
const dialog = document.getElementById("confirmation");
let displayed = null;
let pending = null;
let busy = false;

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

function render(view) {
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
    element("h2", "Entry preconditions (checked again when requested)", statusNode);
    for (const target of ["observe", "protect", "optimise"]) {
        const report = view.preconditions[target];
        element("h3", target + ": " + (report.passed ? "passed" : "not met"), statusNode);
        const list = element("ul", "", statusNode);
        for (const reason of report.reasons) element("li", reason, list);
        if (view.mode === "authority_unknown" && target !== "observe") continue;
        const button = element("button", view.mode === "authority_unknown"
            ? "Confirm Observe" : "Request " + target, actions);
        button.type = "button";
        button.disabled = busy;
        button.addEventListener("click", () => choose(target));
    }
}

async function reload() {
    displayed = null;
    actions.replaceChildren();
    try {
        const response = await fetch("api/mode", {cache: "no-store"});
        if (!response.ok) throw new Error("status unavailable");
        render(await response.json());
    } catch {
        message.textContent = "Could not load current authority. Refresh before requesting a change.";
    }
}

function choose(target) {
    if (busy || !displayed) return;
    // Capture the epoch the owner saw, not a later refreshed value.
    const request = {target, expected_epoch: displayed.mode === "authority_unknown"
        ? null : displayed.epoch};
    if (target === "observe") {
        void submit(request);
        return;
    }
    pending = request;
    document.getElementById("confirm-text").textContent = target === "optimise"
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
    document.getElementById("refresh").disabled = true;
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
        document.getElementById("refresh").disabled = false;
        await reload();
    }
}

document.getElementById("confirm").addEventListener("click", () => {
    const request = pending;
    pending = null;
    dialog.close();
    if (request) void submit(request);
});
document.getElementById("cancel").addEventListener("click", () => {
    pending = null;
    dialog.close();
});
dialog.addEventListener("cancel", () => { pending = null; });
document.getElementById("refresh").addEventListener("click", () => { void reload(); });
void reload().then(() => {
    if (displayed) message.textContent = "Review status before requesting a change.";
});
