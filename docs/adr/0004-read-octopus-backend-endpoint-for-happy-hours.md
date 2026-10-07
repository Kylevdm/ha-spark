# ADR-0004: Read Weekend Happy Hours from the Octopus backend endpoint

Status: Accepted (2026-10-07)

## Context

Octopus Weekend Happy Hours give a free hour of electricity (up to 16 kWh) on a
Sunday. Each week Octopus offers a few one-hour slots (so far four, 11:00 to
15:00 local) and the customer books one or more of them in their account. The
hour is chosen by the customer, so ha-spark can't assume a fixed time. It has to
read the booking before it can plan around it (#237: hold the battery and
charge to the cap while grid energy is free).

A live check against the maintainer's account (2026-10-07, #237) found:

- Offered and booked slots are only exposed by the **backend** GraphQL endpoint
  the Octopus web app uses, `api.backend.octopus.energy/v1/graphql/`, under
  `savingSessions` (`events` and `account.joinedEvents`, with
  `eventType: WEEKEND_HAPPY_HOUR`). Octopus doesn't document this endpoint.
- The Home Assistant Octopus Energy integration reads the same query but
  doesn't expose Happy Hour bookings as entities. Its saving-session event
  entities carry only `TURN_DOWN` events, so ha-spark can't get the bookings
  from HA states.
- The public endpoint ha-spark already uses (`octopus_api_url`, for Intelligent
  dispatches) hasn't been shown to carry them.

The security rules in CLAUDE.md allow outbound calls only to configured
endpoints and treat every third-party payload as untrusted. An undocumented
endpoint can change shape or disappear without notice.

## Decision

ha-spark may read Weekend Happy Hour bookings from the Octopus backend
endpoint, within these limits:

- **Configured, not hard-coded.** The endpoint is a setting (provisionally
  `octopus_backend_api_url`, default `https://api.backend.octopus.energy/v1`),
  next to `octopus_api_url` and kept in sync across `config.yaml`,
  `config.py` `_OPTION_KEYS` and DOCS.md like every other option.
- **Read-only.** Only the `savingSessions` query is sent. ha-spark never sends
  booking or cancel mutations. Booking a slot changes the customer's account
  and stays the customer's choice.
- **Existing credentials.** It authenticates with the same short-lived Kraken
  JWT that `octopus_api_key` already yields. Neither the key nor the token is
  logged or echoed.
- **Degrade, never fail.** The response is validated with pydantic before
  use. Any HTTP error, unexpected shape or missing field means "no Happy Hour
  booked": the plan goes ahead without it, and the failure is logged without
  secrets.

Free Electricity Sessions and Power-ups aren't covered by this decision. They
need an official source (`customerFlexibilityCampaignEvents` on the public
endpoint is the candidate). The third-party JSON feed the HA integration uses
for them is not an acceptable source.

## Alternatives considered

- **Read bookings from HA entities.** Preferred in principle, because ha-spark
  already treats HA as its data source. Rejected for now because the
  integration doesn't expose Happy Hour bookings. Worth revisiting if a future
  integration release adds them.
- **Ask the user to enter the booked hour.** Works without any new endpoint,
  but it is a weekly manual step that is easy to forget, and a forgotten entry
  silently wastes the free hour. Kept as a possible fallback, not the main
  path.
- **Don't support Happy Hours.** Avoids the undocumented endpoint, but leaves
  up to 16 kWh a week of free energy unplanned.

## Consequences

- ha-spark gains a second outbound Octopus endpoint. It is configured, so it
  can be pointed elsewhere or disabled, and it is read-only.
- When Octopus changes the backend schema, Happy Hours quietly stop being
  planned rather than breaking the plan. Health output should report a failed
  read so a silent stop is noticed.
- Planning around a booked hour depends on charging in a daytime cheap slot
  (#236), which depends on starting the horizon at the current slot (#200).
