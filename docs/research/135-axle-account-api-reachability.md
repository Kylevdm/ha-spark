# Research: Axle API reachability for one household account

Issue: [Is any documented Axle site/asset API reachable for a single household account?](https://github.com/Kylevdm/ha-spark/issues/135), part of [Wayfinder: supervised Axle export prototype on the Solis](https://github.com/Kylevdm/ha-spark/issues/128).

Date: 2026-09-12. All requests were read-only. The probes printed only HTTP
status codes and JSON field names. No credential, token, site identifier, asset
identifier, or response value was printed, copied into this file, or committed.

## Answer

The household's Axle Events-Only static token does not authenticate against the
documented site or asset API. A live read-only probe returned HTTP 200 from
`GET /vpp/home-assistant/event`, then HTTP 403 from the site list, asset list,
site flex-events, site price-curve, and asset dispatch-schedule endpoints.

The successful Home Assistant response contained `start_time`, `end_time`,
`import_export`, `updated_at`, and the undocumented `opted_out` field. It still
contained no rate, event identifier, site identifier, or asset identifier.
Because `opted_out` is absent from Axle's first-party contract, ha-spark should
not depend on it.

The first-party documentation answers the contract question:

- The self-service token in an Axle household account is documented only for
  `GET /vpp/home-assistant/event`. Axle says the token controls access to grid
  event data and shows no other use for it. That endpoint is absent from Axle's
  OpenAPI document. The token is available only in Events Only mode.
  [Axle's Home Assistant page](https://vpp.axle.energy/landing/home-assistant)
- The documented site and asset endpoints use Axle's OAuth bearer scheme. The
  standard bearer token is organisation-scoped, lasts one hour, and comes from
  an API username and password. Axle does not say that a household web-account
  login or Axle Events-Only static token can obtain it.
  [Authentication](https://docs.axle.energy/api-reference/auth/token-form)
- Axle also documents a 24-hour, site-scoped component token for end-user
  sessions. An already authenticated API client must mint it for an external
  user in that client's organisation. This is a partner integration path, not
  a self-service route from a household Axle account. The documentation does
  not equate this token with the Axle Events-Only static token or give a
  per-endpoint permission list.
  [Component token](https://docs.axle.energy/api-reference/auth/component-token)

The answer for [Choose the event-source contract for the supervised
Axle prototype](https://github.com/Kylevdm/ha-spark/issues/130) is therefore
settled for this Events-Only account. The four-field Home Assistant endpoint is
the only authenticated per-household route. It has an upcoming window and
direction, but no rate. The static token cannot use the documented site or
asset endpoints.

## What the documented endpoints contain

Even with organisation or component-token access, Axle does not document one
endpoint that returns an upcoming export-event window together with the
household's reward rate.

| Endpoint | Time direction | Price or reward | Fit for this question |
| --- | --- | --- | --- |
| `GET /vpp/home-assistant/event` | The next event's `start_time`, `end_time`, and `import_export` | None | Usable household contract in Events Only mode, but no rate |
| `GET /entities/site/{site_id}/flex-events` | `start_at` and `end_at` for events the site has participated in | Estimated and final gross revenue after participation | Historical settlement, not an advance event feed |
| `GET /entities/site/{site_id}/price-curve` | Half-hour slots from the current settlement period through 23:00 UK time | `price_gbp_per_mwh`, or null when the asset is not participating | An advance market-price curve, but not an event instruction or household reward contract |
| `GET /entities/asset/{asset_id}/todays-dispatch-schedule` | Today's ordered `start_timestamp` and `end_timestamp` periods with an action | None | An Optimised dispatch plan, but no rate |

The schedule response has only `periods`; each period has
`start_timestamp`, `end_timestamp`, and `action`. It is production-only. Axle
returns an empty plan when the battery is not optimised, has no asset model, or
lacks full schedule control or wholesale-market consent. The response does not
say which condition caused the empty result. [Axle OpenAPI 1.4.6](https://api.axle.energy/openapi.json)

The price curve and schedule could be matched by time as an optimisation input.
That would be an inference made by a client. Axle does not document the curve's
`price_gbp_per_mwh` as the household's event reward, and the schedule does not
identify which periods are grid events. The flex-events endpoint reports gross
revenue only after participation. [Get site price curve](https://docs.axle.energy/api-reference/entities/site/price-curve),
[get site flex events](https://docs.axle.energy/api-reference/entities/site/flex-events)

## Difference between dispatch modes

Axle documents two VPP modes. Events occur in both. Events Only sends commands
only during grid-stress periods. Optimised dispatch incorporates events into a
daily battery schedule. Axle sends daily schedules only in Optimised dispatch
mode. [VPP overview](https://docs.axle.energy/workflows/axle-vpp/overview)

This changes which documented feed could exist, but not the access result:

- In Events Only mode, the household can generate the Axle token used by the
  Home Assistant integration. Its documented response has `start_time`,
  `end_time`, `import_export`, and `updated_at`. It has no event rate.
- In Optimised dispatch mode, the Home Assistant section is not available.
  An onboarded battery with full schedule control and wholesale-market consent
  can have a daily schedule, but the endpoint still requires Axle API bearer
  auth and its periods have no price field.
- Axle's OEM integration maps Events Only to `vpp_limited_control` and full
  control to `full_asset_schedule_control`. That workflow is for battery
  manufacturers operating their own cloud platform, not a household account.
  [VPP integration](https://docs.axle.energy/workflows/axle-vpp/integration)

## Live probe evidence

### Local account-access evidence

The process environment and `/home/kyle/ha-agent/.env` were initially inspected
by key name only:

- No key name matched Axle, VPP, `site_id`, or `asset_id`.
- `HA_URL` and `HA_TOKEN` were present. Their values were used only for
  read-only Home Assistant requests.

The account owner then supplied `AXLE_HA_TOKEN` locally. Its value was passed to
curl through standard input, never printed or placed in the process argument
list. No organisation bearer, component bearer, `site_id`, or `asset_id` was
supplied.

The Home Assistant requests produced these results:

| Request | HTTP status | Returned field presence |
| --- | ---: | --- |
| `GET /api/states` | 200 | Axle entities for start time, end time, import/export, updated-at, derived window state, countdowns, flags, update metadata, and a calendar event |
| `GET /api/config/config_entries/entry` | 200 | One `axle_vpp` entry; the response had entry metadata fields but no `data` or `options` fields |
| `GET /api/config/config_entries/entry/{entry_id}` | 405 | No fields recorded |
| `GET /api/hassio/addons` | 401 | No fields recorded |

No Axle entity returned `site_id` or `asset_id` as an attribute field. These
checks establish that the integration is installed and the household event
fields reach Home Assistant. The later direct Axle probes below test the token
against both endpoint families.

The completed account-specific credential matrix is:

| Credential | Endpoint family | Account-specific result |
| --- | --- | --- |
| Axle Events-Only static token | `/vpp/home-assistant/event` | HTTP 200; fields were `end_time`, `import_export`, `opted_out`, `start_time`, and `updated_at` |
| Axle Events-Only static token | Documented `/entities/site` and `/entities/asset` GETs | HTTP 403 from every tested endpoint |
| Organisation bearer | Documented `/entities/site` and `/entities/asset` GETs | Not tested because no organisation credential or bearer was available |
| Component bearer | Documented `/entities/site` and `/entities/asset` GETs | Not tested because minting one requires an organisation-authenticated API client |
| Any Axle API bearer | Real site and asset resource GETs | Not tested because no `site_id` or `asset_id` was available |

### Axle production endpoint evidence

The public OpenAPI request returned HTTP 200 and contained all documented paths
and schemas cited above. The following production GET probes used either no
Authorization header or the fixed non-credential string
`credential-not-present`. Response bodies were discarded.

| Probe | Auth | HTTP status |
| --- | --- | ---: |
| `/vpp/home-assistant/event` | none | 401 |
| `/vpp/home-assistant/event` | fixed non-credential | 401 |
| `/entities/site?limit=1` | none | 401 |
| `/entities/site?limit=1` | fixed non-credential | 401 |
| `/entities/site/{zero-uuid}/flex-events` | fixed non-credential | 401 |
| `/entities/site/{zero-uuid}/price-curve` | fixed non-credential | 401 |
| `/entities/asset/{zero-uuid}/todays-dispatch-schedule` | fixed non-credential | 401 |

These probes confirm that production protects both API families. The real
Events-Only token then produced this result:

| Probe | HTTP status | Returned field presence |
| --- | ---: | --- |
| `/vpp/home-assistant/event` | 200 | `end_time`, `import_export`, `opted_out`, `start_time`, `updated_at` |
| `/entities/site?page_size=1` | 403 | `detail` only |
| `/entities/asset?page_size=1` | 403 | `detail` only |
| `/entities/site/{zero-uuid}/flex-events` | 403 | `detail` only |
| `/entities/site/{zero-uuid}/price-curve` | 403 | `detail` only |
| `/entities/asset/{zero-uuid}/todays-dispatch-schedule` | 403 | `detail` only |

This directly rules out cross-token compatibility for the tested documented
site and asset endpoints. A separate organisation or component bearer could
still access them, but that is a partner API route and no such credential was
available for this household test.

## Sources

All web sources are Axle first-party pages, accessed 2026-09-12:

1. [Home Assistant integration](https://vpp.axle.energy/landing/home-assistant)
2. [Authentication](https://docs.axle.energy/api-reference/auth/token-form)
3. [Component token](https://docs.axle.energy/api-reference/auth/component-token)
4. [Get all sites](https://docs.axle.energy/api-reference/entities/site/list)
5. [Get site flex events](https://docs.axle.energy/api-reference/entities/site/flex-events)
6. [Get site price curve](https://docs.axle.energy/api-reference/entities/site/price-curve)
7. [VPP overview](https://docs.axle.energy/workflows/axle-vpp/overview)
8. [VPP integration](https://docs.axle.energy/workflows/axle-vpp/integration)
9. [Axle OpenAPI 1.4.6](https://api.axle.energy/openapi.json)

Repository context came from
`docs/research/129-axle-event-contract.md` at commit `433cf3d` on branch
`research/129-axle-event-contract`.
