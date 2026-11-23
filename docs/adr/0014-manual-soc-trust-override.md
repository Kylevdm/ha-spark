# ADR-0014: The owner may override SoC trust for their own manual charge edits

Status: Accepted (#766; review follow-ups 2026-11-23).

## Context

An invalid SoC blocks every SoC-derived write. During the #704 live run on
2026-11-22 the native SoC trust stuck at `implausible`: SoC froze at 51 % while
energy moved. The owner, standing at the inverter during a cheap dispatch,
could not raise the charge current by hand, because owner charge edits were
SoC-gated like plan writes. 0.42.1 shipped an opt-in override in a hurry, with
no review. The post-hoc review found that the override also let peers on the
same connection be raised to a latent request the owner never made, and that a
supply-guard cut stuck for the rest of the session.

## Decision

`start_manual_control` takes `override_soc_trust` (default false). It is
accepted only with `keep_protection: false`; otherwise the call is refused with
`soc_override_needs_handover`. It lasts as long as the session, whatever its
duration, including `until_resumed`. There is no durable option: the owner opts
in per session. It is native only; the add-on has no manual control.

While the session lasts, untrusted SoC does not block, on that inverter:

- the owner's own charge-current and charge-window edits; and
- a write of a charge current no higher than the owner's own current edit of
  that inverter in this epoch. This is how the supply guard restores the
  owner's current after a cut.

The boundary tracks the owner's edits in memory as `(epoch, amps)` per
inverter (`NativeChargeBoundary.owner_currents`). An edit refused before its
write is forgotten. Peers the owner did not edit, and any current above the
owner's edit, stay refused with `untrusted_soc`. One predicate,
`NativeChargeBoundary._owner_soc_override`, decides every case.

Owner discharge edits were never SoC-gated, so the override does not change
them.

Everything else still applies: the session's mode gate, authority and epoch
rechecks, the device, bank and site limits, the guard clamp, grid-charge
permission and exact read-back. The planner, fallback and dated windows still
refuse on untrusted SoC.

## Consequences

- The "invalid SoC blocks every SoC-derived write" rule gains one exception,
  named in `CLAUDE.md`.
- A refused raise of a peer stops the owner's transaction, as any refused peer
  write does. The owner's edit is then refused with `untrusted_soc` until the
  peer's latent request is gone.
- After a Home Assistant restart, or when an edit was made from the inverter's
  own app, the guard has no owner edit on record and restores stay refused
  until the owner sets the current again.
- The manual-session sensor shows `override_soc_trust`, so an active override
  is visible.
