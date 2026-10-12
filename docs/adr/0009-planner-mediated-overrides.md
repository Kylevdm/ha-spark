# Planner-mediated overrides

Status: accepted (2026-10-12), confirmed by the owner in [Manual override: force charge or stop charging by hand](https://github.com/Kylevdm/ha-spark/issues/255). This is a v1 planning contract, not implemented behavior. It settles the individual override command that [ADR-0005](0005-integration-authority-and-operating-modes.md) left distinct from temporary manual control, within the lifecycle of [ADR-0008](0008-operating-mode-and-authority-lifecycle.md).

## Decision

An override is an owner instruction to the plan, not a device command and not a handover. It exists only in Optimise. ha-spark keeps ownership of the controls, the planner treats the override as a hard constraint, and it plans the rest of the horizon around it (holds, flexibility events, charging allocation). Writes still go through the normal plan apply: limits, the supply guard, SoC integrity, read-back and the authority epoch all apply unchanged.

We rejected two alternatives:

- **Integration-executed fixed command.** It would work without the add-on, but in Optimise it competes with the plan. The plan would have to stop, which makes it a manual session in all but name.
- **Shortcut over temporary manual control.** It reuses an agreed contract, but it hands over the whole site, so one "charge now" press stops all optimisation.

The cost is that an override does nothing without the add-on. Fallback is the safety net then, as it is for the plan itself.

## Commands

| Kind | Parameters | Ends when |
|---|---|---|
| Charge now | target SoC (default 100%), end time | target reached or end time, whichever first |
| Don't charge | end time | end time |
| Keep battery (no discharge) | end time | end time |

There is no rate parameter. Charge now charges as fast as the charging allocation and supply guard allow. End-time choices mirror manual control: one hour (default), until tomorrow at the household-clock time from ADR-0008 (default 07:00), or a custom time no later than that. There is no "until cancelled". The owner may cancel at any time, and every end triggers a fresh replan. Forced export is out of scope for v1, because ADR-0007 bars unattended export without a device-resident bound.

There is one override at a time for the whole site, and a new one replaces the current one after confirmation. Per-bank overrides would reopen the per-bank control that ADR-0008 ruled out of v1.

## Precedence

1. Device, bank and site limits, and the supply guard.
2. An applicable protection hold. Where a hold disables the whole inverter, Charge now cannot charge during it, and the UI says why.
3. The override.
4. Flexibility events. If an override would skip or underfund an armed event, the confirmation shows the consequence before the owner accepts. Once confirmed, the override wins.
5. The rest of the plan.

## State and lifecycle

The override is a plan input, so it lives in the add-on's durable store, not the integration's authority store. It survives restarts and does not change the authority epoch. It is **suspended**, with its end time still running and the reason shown, while SoC is untrusted or the fallback charging schedule is in force. Fallback ignores it, so fallback may charge during a Don't charge override. When planning resumes, an unexpired override applies again. Leaving Optimise (to Protect, Observe, or manual control) cancels the override, with a notice.

## Who and where

Overrides come only from ingress (admin-only) and the CLI, through one function. Automations, scripts, non-admin callers and the LLM cannot issue them. Chat may explain an override and link to the form. The integration exposes a read-only sensor for the current override and **no service**: a service would only forward to the add-on, and it would be one more way in to authenticate. Start, end, suspension and cancellation are notified through the household's notify target.

In Observe, Protect and manual control, the override form is disabled with an explanation. In Protect or manual control, the owner edits the integration's charging entities directly, under ADR-0008's validation. A convenience form for those writes is out of scope for v1.
