# Research: Axle API reachability for one household account

Issue: [Is any documented Axle site/asset API reachable for a single household account?](https://github.com/Kylevdm/ha-spark/issues/135), part of [Wayfinder: supervised Axle export prototype on the Solis](https://github.com/Kylevdm/ha-spark/issues/128).

Date: 2026-09-12. All requests were read-only. The probes printed only HTTP
status codes and JSON field names. No credential, token, site identifier, asset
identifier, or response value was printed, copied into this file, or committed.

## Interim answer

No documented Axle site or asset endpoint is verified as reachable with this
household's Axle Events-Only static token. The account-specific cross-token
question remains unresolved and this evidence is not sufficient to close the
ticket.

The account-specific test could not be completed because the Axle Events-Only
static token, an Axle organisation credential, `site_id`, and `asset_id` were
not available to the research process. The repository's local environment has
a separate Home Assistant access token, but it has no Axle or VPP credential
variables and no site or asset identifier variables. Home Assistant exposes the
installed Axle integration and its event entities, but its read APIs did not
expose the Axle Events-Only token or Axle identifiers.

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

The practical answer for [Choose the event-source contract for the supervised
Axle prototype](https://github.com/Kylevdm/ha-spark/issues/130) is therefore
unchanged. The four-field Home Assistant endpoint is the only documented and
locally evidenced per-household route. It has an upcoming window and direction,
but no rate. No first-party contract establishes that this household can use a
documented site or asset endpoint.

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

The process environment and `/home/kyle/ha-agent/.env` were inspected by key
name only:

- No key name matched Axle, VPP, `site_id`, or `asset_id`.
- `HA_URL` and `HA_TOKEN` were present. Their values were used only for
  read-only Home Assistant requests.

The Home Assistant requests produced these results:

| Request | HTTP status | Returned field presence |
| --- | ---: | --- |
| `GET /api/states` | 200 | Axle entities for start time, end time, import/export, updated-at, derived window state, countdowns, flags, update metadata, and a calendar event |
| `GET /api/config/config_entries/entry` | 200 | One `axle_vpp` entry; the response had entry metadata fields but no `data` or `options` fields |
| `GET /api/config/config_entries/entry/{entry_id}` | 405 | No fields recorded |
| `GET /api/hassio/addons` | 401 | No fields recorded |

No Axle entity returned `site_id` or `asset_id` as an attribute field. These
checks establish that the integration is installed and the household event
fields reach Home Assistant. They do not recover the Axle token and do not test
whether it works on another Axle endpoint.

The account-specific credential matrix remains:

| Credential | Endpoint family | Account-specific result |
| --- | --- | --- |
| Axle Events-Only static token | `/vpp/home-assistant/event` | Indirectly evidenced by the installed entities; the token itself was unavailable for a direct probe |
| Axle Events-Only static token | Documented `/entities/site` and `/entities/asset` GETs | Not tested because Home Assistant did not expose the token |
| Organisation bearer | Documented `/entities/site` and `/entities/asset` GETs | Not tested because no organisation credential or bearer was available |
| Component bearer | Documented `/entities/site` and `/entities/asset` GETs | Not tested because minting one requires an organisation-authenticated API client |
| Any Axle API bearer | Real site and asset resource GETs | Not tested because no `site_id` or `asset_id` was available |

### Axle production auth-gate evidence

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

These probes confirm that production protects both API families. They do not
answer cross-token compatibility. Only the real Axle Events-Only static token
and a real API bearer can answer that account-specific question.

## Safe completion procedure for the account owner

The remaining test needs the account owner to supply credentials locally. Do
not paste them into an issue, terminal transcript, command argument, or shell
with tracing enabled. Place an already obtained bearer in an environment
variable, then make GET requests whose output filter prints only the status and
field names. Feed the Authorization header to curl through standard input so it
does not appear in the process argument list. Do not call an auth or onboarding
POST as part of this test.

Required local variables:

- `AXLE_HA_TOKEN` for the static Events Only token.
- `AXLE_API_BEARER` for an already obtained organisation or component bearer,
  if the account has one.
- `AXLE_SITE_ID` and `AXLE_ASSET_ID`, if known.

Use this pattern for each token and endpoint:

```bash
set +x
body_file=$(mktemp)
trap 'rm -f "$body_file"' EXIT
status=$(
  printf 'header = "Authorization: Bearer %s"\n' "$AXLE_HA_TOKEN" |
    curl -sS --config - --output "$body_file" --write-out '%{http_code}' \
      --header 'Accept: application/json' \
      'https://api.axle.energy/entities/site?limit=1'
)
printf 'status=%s\n' "$status"
jq 'if type == "object" then keys | sort else type end' "$body_file"
rm -f "$body_file"
trap - EXIT
```

Repeat the same GET with `AXLE_API_BEARER`. If site and asset identifiers are
available, probe `flex-events`, `price-curve`, and
`todays-dispatch-schedule`. For a 200 response, print only these structures:

```bash
jq '{top_level_fields: (keys | sort), event_fields: ((.events[0]? // {}) | keys | sort)}'
jq '{top_level_fields: (keys | sort), price_fields: ((.half_hourly_traded_prices[0]? // {}) | keys | sort)}'
jq '{top_level_fields: (keys | sort), period_fields: ((.periods[0]? // {}) | keys | sort)}'
```

A 200 from a documented endpoint with `AXLE_HA_TOKEN` would establish token
reuse. A 401 or 403 would rule it out for that endpoint. A 404 is not enough to
separate an unknown identifier from lack of authorisation because Axle
documents both causes together on several resource endpoints.

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
