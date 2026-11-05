# Owner-aware strategy object

ha-spark does not resolve "who owns this site" once per pass into a strategy object that the scheduler, sources, write gate, server, contract and health code consult instead of reading `retirement_status(settings)` themselves.

## Why this is out of scope

The repeated reads look like duplication, but on the write path they are a fence. `WriteGate.mode_for` (`ha_spark/devices/execution.py`) reads the durable retirement marker on **every write**, and `WriteGate.recheck` repeats that under the bus lock just before the write goes out. If the marker appears mid-pass (the integration takes ownership), the very next write is refused, even though the pass was planned while the add-on still owned the site.

A strategy object "resolved once per pass" would turn that write-time check into a plan-time one, so a write planned before the handover could still go out after it. The CLAUDE.md actuation invariants forbid weakening that fence.

Keeping write-time reads and wrapping only the scheduler's `if native` branches in an object would add an abstraction nobody asked for, and the branches stay readable where they are. The ownership split is also a standing design: add-on-owned sites remain for legacy and AlphaESS installs, so there is no single "native path" to collapse onto.

Module-size problems in the native code (the coordinator, the charge-write boundary) are handled by move-only splits, not by this.

## Prior requests

- #597: "Refactor: one owner-aware strategy for native vs add-on paths" (finding S1 of the #591 review)
