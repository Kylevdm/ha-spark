# Zappi reliability against Octopus dispatches

Issue: [#141](https://github.com/Kylevdm/ha-spark/issues/141)  
Date: 2026-09-17  
Status: negative result. No implementation is recommended.

## Question

Can the Zappi Home Assistant state be trusted against Octopus Intelligent
dispatch data over a representative period, including missed transitions,
stale states, and gaps? If so, should the inverter hold also cover manual,
non-dispatched, or ECO+ charging?

## Verdict

The repository does not contain a representative paired Zappi and Octopus
history, and this session had no authorised live Home Assistant history access.
There are therefore no defensible counts for missed transitions, stale states,
or gaps.

The code-contract evidence is not strong enough to admit Zappi status as an
inverter safety input. Keep the hold condition dispatch-only and close this
ticket as a negative result. Do not extend `_controlled_windows` or its
reconcile input from the current Zappi status sensor.

This is not a measured claim that every Zappi transition is unreliable. It is
a decision that reliability has not been demonstrated, while the available
contracts contain known failure and ambiguity modes.

## Evidence boundary

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| Local `ha-spark` source | The current data path, polling cadence, state classification, and trust handling | Real household transition accuracy |
| Community `ha-myenergi` source | The integration is cloud-polled, exposes status and plug states, and documents an Octopus incompatibility warning | A vendor reliability guarantee or an error rate |
| myenergi support documentation | The API is not officially supported | Whether this particular installation misses or delays reports |
| myenergi Zappi manual | ECO+ pause/resume and boost behaviour are normal device behaviour | Whether Home Assistant observes each behaviour promptly |
| Octopus API documentation | The dispatch source has a defined current query and dispatch fields | Whether the Zappi state agrees with it at a household |
| Local history | No committed paired Zappi/dispatch dataset was found | Any representative-period conclusion |

## Local code contract

The preset identifies the Zappi status entity, but the repository does not
ship the Zappi integration itself. [`sources.py`](../../ha_spark/energy/sources.py#L42-L43)
classifies `charging`, `delivering`, `boosting`, and `diverting` as active. In
[`gather_inputs`](../../ha_spark/energy/sources.py#L276-L309), the status is read
as an ordinary HA state and reduced to a boolean. A failed read becomes
`None`, which becomes `False`; no `last_reported` age or source-trust bit is
carried with `ev_charging`.

The HA model does preserve `last_reported` as a possible freshness signal
([`ha/models.py`](../../ha_spark/ha/models.py#L14-L26)), but the Zappi path does
not use it. The dispatch path has separate trusted/untrusted handling and
distinguishes an unreadable source from an empty dispatch list
([`sources.py`](../../ha_spark/energy/sources.py#L226-L252)). That asymmetry is
safe for dispatch holds and is absent from the Zappi status path.

The daemon replans once per local half-hour slot
([`scheduler.py`](../../ha_spark/energy/scheduler.py#L1-L9),
[`scheduler.py`](../../ha_spark/energy/scheduler.py#L87-L99)). Its independent
per-minute reconcile re-reads the dispatch entity, but intentionally reuses
the last plan's holds on the `octopus_intelligent` path
([`scheduler.py`](../../ha_spark/energy/scheduler.py#L411-L419),
[`scheduler.py`](../../ha_spark/energy/scheduler.py#L619-L625)). It does not
re-read Zappi status. Any future Zappi hold input would therefore need its own
freshness, outage, and boundary policy before it could be used by the safety
reconcile.

The local SQLite store is explicitly a half-hourly Octopus consumption store
for cost backtesting, not a Zappi or dispatch-event store
([`store.py`](../../ha_spark/energy/store.py#L1-L6)). The onboarding path can
import a selected HA statistic, but no paired Zappi/dispatch history has been
captured in this repository ([`onboarding.py`](../../ha_spark/energy/onboarding.py#L1-L8)).

## Zappi integration contract

The reference integration is the community HACS project
[`CJNE/ha-myenergi`](https://github.com/CJNE/ha-myenergi). Its README lists
Zappi charger states `Paused`, `Charging`, `Boosting`, and `Completed`, plus
plug states such as `EV Connected`, `Waiting for EV`, and `EV Disconnected`
([integration README](https://github.com/CJNE/ha-myenergi#readme)). The
repository's active-state set does not include `Paused` or `Completed`, and it
does not use plug status as a second condition. That is a semantic choice, not
an observed reliability measurement.

The integration manifest declares `cloud_polling`, with a default 60-second
coordinator interval that can be changed in its options, and depends on
`pymyenergi==0.2.3`
([manifest](https://github.com/CJNE/ha-myenergi/blob/main/custom_components/myenergi/manifest.json#L251-L262),
[coordinator source](https://github.com/CJNE/ha-myenergi/blob/main/custom_components/myenergi/__init__.py#L551-L650)).
An update exception is raised as `UpdateFailed`
([coordinator source](https://github.com/CJNE/ha-myenergi/blob/main/custom_components/myenergi/__init__.py#L654-L689)),
so transport failures can become an unavailable coordinator state. The source
does not provide a guarantee about the age of a successfully returned device
value.

The integration's own README says, "This integration is incompatible with
Octopus" and warns that it will not function correctly when Octopus controls
the devices ([integration README](https://github.com/CJNE/ha-myenergi#readme)).
That warning is directly relevant to this ticket. It rules out treating the
integration as an authoritative confirmation of Octopus-controlled charging.

myenergi's own support article says its API exists but is "not officially
supported" and was initially developed for internal use
([myenergi API support article](https://support.myenergi.com/hc/en-gb/articles/4404522743313-myenergi-API)).
There is no first-party freshness, delivery, or availability contract here
that could replace the missing household measurement.

The integration has also had a reported availability bug in which a temporary
`None` value for a Zappi property left a sensor unavailable until restart. The
issue was closed with a fix, so it is evidence of a historical failure mode,
not proof that the current release has the same defect
([issue #747](https://github.com/CJNE/ha-myenergi/issues/747)).

## Device-mode semantics

The official Zappi manual says ECO+ pauses charging when imported power is too
high and resumes when sufficient surplus is available. It also documents a
configurable start/stop delay, so a short `Paused` interval can be normal
operation rather than an integration failure
([Zappi operation and installation manual, pp. 11-12](https://support.myenergi.com/hc/article_attachments/21619738055313)).

The same manual says Manual Boost charges at the maximum rate until its energy
amount is reached, then returns to ECO or ECO+, and that Boost Timer can draw
from the mains regardless of surplus
([manual, pp. 13-15](https://support.myenergi.com/hc/article_attachments/21619738055313)).
This confirms that non-dispatched charging is physically possible and that a
single status string does not identify the energy source or the cause of a
transition. It does not show whether the HA entity reports those transitions
without delay.

## Octopus dispatch reference

Octopus's current GraphQL documentation defines `flexPlannedDispatches` as
planned device dispatches in time order, keyed by a SmartFlex `deviceId`, with
`start`, `end`, `type`, and `energyAddedKwh`
([Octopus GraphQL query reference](https://docs.octopus.energy/graphql/reference/queries/)).
The older `plannedDispatches(accountNumber: ...)` query used by
[`ha_spark/energy/octopus.py`](../../ha_spark/energy/octopus.py#L204-L283) is
deprecated. Kraken's first-party announcement says it was replaced because it
only supported one device per account
([Kraken deprecation announcement](https://announcements.kraken.tech/announcements/public/166/)).

This matters to a future evidence run. The comparison series must use one
stable Octopus dispatch contract, retain the exact dispatch window, and record
query errors and unavailable responses separately from an empty dispatch list.
The current repository code already treats an Octopus API error as an
untrusted dispatch result ([`octopus.py`](../../ha_spark/energy/octopus.py#L241-L283),
[`sources.py`](../../ha_spark/energy/sources.py#L292-L304)).

## Missing evidence

The following evidence is required before a Zappi-derived hold could be
considered:

1. A privacy-local, representative period with both an Octopus dispatch series
   and Zappi observations. The observations need event timestamps, state,
   availability, and a charge-power or session-energy signal.
2. A pairing rule that compares each Zappi start and stop with the Octopus
   dispatch window, allowing for the Zappi's documented ECO+ delay and the
   integration's configured poll interval.
3. Aggregate results only: observation coverage, gap durations, stale-value
   durations, missed starts, missed stops, false active intervals, and counts
   split between dispatched, manual/boost, and ECO+ sessions. No raw timestamps
   or household schedule should enter the repository.
4. A defined fail-closed rule for unavailable, stale, or semantically
   ambiguous Zappi data. A missing Zappi read cannot mean "not charging" while
   the inverter is allowed to discharge.

Neither the repository's committed documents nor its local application store
contains this paired dataset. I did not access a live HA recorder because no
authorised token or export was provided, and I did not inspect or print raw
telemetry.

## Recommendation

Close [#141](https://github.com/Kylevdm/ha-spark/issues/141) with this negative
result. Leave the dispatch-only hold from [#140](https://github.com/Kylevdm/ha-spark/issues/140)
in place. A later evidence ticket may revisit the decision after collecting
the aggregate paired history under the privacy and fail-closed rules above.

No production code, issue body, credential, token, raw telemetry, or occupancy
revealing time series was changed or included.

## Sources and method

Local sources were read-only inspections of the paths linked above, excluding
runbook CSV/log telemetry. External sources were limited to first-party
myenergi documentation, the Zappi manual, Octopus/Kraken API documentation,
and the source/issues of the community Home Assistant integration used by the
reference configuration. Accessed 2026-09-17.
