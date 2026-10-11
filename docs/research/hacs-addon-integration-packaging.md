# HACS integration packaged alongside the add-on, and add-on/integration discovery

Date: 2026-10-08
Ticket: [#252](https://github.com/Kylevdm/ha-spark/issues/252), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).

Question: can a HACS custom integration (`custom_components/ha_spark`) live in
the same repo as the add-on (`repository.yaml` + `ha_spark_addon/`)? What do
HACS validation, `hacs.json` and the release/tag rules require? How should the
integration find and authenticate to the add-on's API? What do established
add-on + integration pairs do, and how does versioning keep the two compatible?

Source code is cited at pinned commits:
Supervisor `9ce1060b`, HA Core `f6f55350`, `home-assistant/addons` `014df506`,
`music-assistant/server` `7e367084`, `music-assistant/home-assistant-addon` `2a1187bd`,
`esphome/esphome` `3c634ecf`, `esphome/home-assistant-addon` `2df5fa4e`,
`oetiker/byonk` `133c5286`.

## TL;DR

- **Yes, one repo works.** The Supervisor store and HACS look at disjoint files.
  There is a live precedent: `oetiker/byonk` ships `repository.yaml`, an add-on
  under `homeassistant/byonk/` and `custom_components/byonk/` in one repo. Its
  release workflow refuses to publish unless the add-on, the integration
  manifest and the core package all carry the same version.
- **HACS needs:** `hacs.json` at the root (`name` is the only required key), a
  repo description, topics, issues enabled, brand assets, and an integration
  `manifest.json` with `domain`, `documentation`, `issue_tracker`, `codeowners`,
  `name` and `version`. For versioning HACS reads **GitHub Releases, not tags**.
  ha-spark today has tags but no Releases, no repo description and no topics, so
  three changes are needed before the HACS action can pass.
- **Discovery:** use Supervisor discovery. Add `discovery: [ha_spark]` to
  `config.yaml`. The add-on POSTs `http://supervisor/discovery` with
  `{"service": "ha_spark", "config": {host, port, auth_token}}`. HA Core then
  starts a config flow for **the domain named by `service`**, with source
  `hassio`, which the integration handles in `async_step_hassio`. This works for
  custom integrations. It is the Music Assistant pattern, which passes a bearer
  token in the discovery config.
- **Not ingress.** No established pair routes integration-to-add-on traffic
  through ingress. Z-Wave JS, Music Assistant, ESPHome and Byonk all connect
  directly to `host:port` on the internal network. This contradicts the premise
  in `ha_spark/api/server.py`'s docstring (see the security finding below).
- **Versioning:** keep both in lockstep. Use one `vX.Y.Z` tag, one GitHub
  Release, and the same version in `config.yaml`, `manifest.json` and
  `pyproject.toml`, enforced by a test. Add a runtime API-version handshake,
  because the two halves update independently: Supervisor and HACS are
  separate update channels.

## 1. Can both live in one repo?

**Supervisor store side.** The store finds add-ons by globbing
`**/config.*` across the whole cloned repo. It skips any path with a part that
starts with `.` or equals `rootfs`, and keeps only configuration suffixes
(json/yaml/yml) (`supervisor/store/data.py:_find_app_configs`). A
`custom_components/ha_spark/` tree contains `manifest.json`, not `config.*`, so
the store ignores it.

> **Gotcha:** any future `config.yaml`/`config.json` anywhere in the repo,
> such as a test fixture or a docs example, would be picked up as an add-on
> candidate. Python files like `ha_spark/config.py` are safe because `.py` is
> not a configuration suffix.

**HACS side.** HACS manages only the first subdirectory of `custom_components/`
("There must only be one integration per repository"). All of the
integration's runtime files must live under
`custom_components/INTEGRATION_NAME/`
([HACS: integration](https://hacs.xyz/docs/publish/integration/)). `hacs.json`
"must be located in the root of your repository"
([HACS: start](https://hacs.xyz/docs/publish/start/)). HACS does not read
`repository.yaml` or `ha_spark_addon/`.

**Precedent.** `oetiker/byonk` has, at its root, `repository.yaml`,
`homeassistant/byonk/config.yaml` (the add-on, with `discovery: [byonk]`) and
`custom_components/byonk/`. Its design notes say its release workflow fails
unless `Cargo.toml`, `custom_components/byonk/manifest.json` and the add-on
`config.yaml` agree on the version
(`docs/superpowers/specs/2026-08-19-addon-installs-integration-design.md`).
Byonk has since dropped HACS. The add-on now writes
`custom_components/byonk/` into HA's config dir itself, because the HACS
default-store PR sat in the queue (same spec). See §6.

The core-integration pairs (Z-Wave JS, Music Assistant, ESPHome) do **not**
share a repo. Their integrations live in `home-assistant/core` and their
add-ons in separate repos, so they are not a precedent for the repo layout,
only for discovery and the API handshake.

## 2. HACS requirements

From [HACS: start](https://hacs.xyz/docs/publish/start/),
[HACS: integration](https://hacs.xyz/docs/publish/integration/),
[HACS: action](https://hacs.xyz/docs/publish/action/) and
[HACS: include](https://hacs.xyz/docs/publish/include/):

| Requirement | ha-spark today |
|---|---|
| Repo description ("needs to have a description") | **Missing** (empty) |
| Repo topics | **Missing** |
| Issues enabled | Yes |
| README in root | Yes |
| `hacs.json` in root, `name` required | Missing |
| `manifest.json` with `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version` | n/a (no integration yet) |
| Brand assets: "a brand directory with at least an icon.png" | n/a |
| GitHub **Releases** ("Just publishing tags is not enough, you need to publish releases") | **Tags only**, no Releases (`gh release list` is empty) |

Useful optional `hacs.json` keys:

- `homeassistant`: the minimum HA version.
- `hide_default_branch`: don't offer `master` as an install target. Recommended,
  so HACS users only get tagged builds.
- `zip_release` + `filename`: serve a release asset instead of the tree.
- `persistent_directory`.

**Versions in HACS.** "If the repository uses GitHub releases, the tag name
from the latest release is used to set the remote version." Without releases,
HACS falls back to the 7-char commit SHA of the default branch. HACS offers the
5 latest releases plus the default branch.

**Validation CI.** `hacs/action@main` with `category: integration` checks
`archived`, `brands`, `description`, `hacsjson`, `images`, `information`,
`issues` and `topics`. Each check can be ignored with `with.ignore`, but
default-store inclusion requires the action to pass "without any errors or
ignores", plus a passing **hassfest** run and at least one Release
([HACS: include](https://hacs.xyz/docs/publish/include/)). A custom repository
(the user adds the URL in HACS) doesn't need default-store inclusion.

**Brands.** Since HA 2026.3, a custom integration can ship
`custom_components/<domain>/brand/icon.png` (and `logo.png`, dark and `@2x`
variants) locally, and HA serves these ahead of the brands CDN
([HA dev blog 2026-02-24](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api),
[brand images](https://developers.home-assistant.io/docs/core/integration/brand_images)).
The HACS doc says only "a brand directory in your repository", with no exact
path. *Uncertain:* whether the HACS UI itself shows local brand images. One
community report says it did not, at some point. Confirm on the first install.

**Manifest requirements (flagged).** The integration should not depend on the
`ha_spark` pip package. That package pulls in scikit-learn and numpy through
`[habits]`, and HA installs manifest `requirements` into Core's own
environment. Keep the integration self-contained, with a small vendored client
for the API contract. *Uncertain:* whether HA still accepts `git+` URL
requirements for custom integrations. Not verified; avoid them anyway.

## 3. How the integration finds and authenticates to the add-on

### Supervisor discovery mechanics (primary source)

1. The add-on lists the service name in `config.yaml` `discovery:` ("A list of
   services that this app provides for Home Assistant",
   [add-on config](https://developers.home-assistant.io/docs/add-ons/configuration/)).
2. The add-on POSTs `http://supervisor/discovery` with `{"service": str,
   "config": dict}`, using `SUPERVISOR_TOKEN`. The Supervisor rejects the call
   with `APIForbidden` unless `service in app.discovery`
   (`supervisor/api/discovery.py:set_discovery`). The service name is a
   free-form `str`, not a hard-coded list (`supervisor/discovery/validate.py`).
   `/discovery.*` is in the security middleware's bypass set, so it doesn't
   need `hassio_api: true` (`supervisor/api/middleware/security.py:105`).
3. Messages are deduplicated on `(app, service)`, because `config` and `uuid`
   are `compare=False`. Re-posting on every start is idempotent, and changed
   config updates the stored message (`supervisor/discovery/__init__.py:send`).
   Messages persist in the Supervisor's discovery file. If Core is down, the
   push is skipped and Core re-reads the list at `EVENT_HOMEASSISTANT_START`
   (`core/components/hassio/discovery.py:async_setup_discovery_view`).
4. Core creates `discovery_flow.async_create_flow(hass, data.service,
   context={"source": SOURCE_HASSIO}, data=HassioServiceInfo(config, name,
   slug, uuid))` (`core/components/hassio/discovery.py:async_process_new`).
   **`service` is used directly as the integration domain**, and nothing in
   `helpers/discovery_flow.py` restricts this to core integrations. So
   `service: ha_spark` triggers `ConfigFlow.async_step_hassio` in
   `custom_components/ha_spark`. Byonk relies on exactly this. Removing the
   discovery message removes the matching `SOURCE_HASSIO` config entry
   (`async_process_del`), and deleting the entry triggers rediscovery
   (`_handle_config_entry_removed`).
5. **Confidentiality of the discovery config.** Only Home Assistant can list or
   read discovery messages: `list_discovery` and `get_discovery` are
   `@require_home_assistant` (`supervisor/api/discovery.py`). This is why
   Music Assistant can put a bearer token in `config`. Other add-ons cannot
   read it.

### What established pairs do

| Pair | `discovery:` | Payload | How the integration connects | Auth |
|---|---|---|---|---|
| **Z-Wave JS** (`home-assistant/addons/zwave_js`) | `[zwave_js]` | `{host: $(hostname), port: 3000}` (`rootfs/etc/services.d/zwave_js/discovery`) | `ws://host:3000` directly, not through ingress; `async_step_hassio` checks `discovery_info.slug == ADDON_SLUG`, probes the server, sets the unique_id from the device's home_id, then shows `hassio_confirm` (`core/components/zwave_js/config_flow.py:779`) | None on the internal port |
| **Music Assistant** (`music-assistant/home-assistant-addon`) | `[music_assistant]` | `{host: $HOSTNAME, port: INGRESS_SERVER_PORT, auth_token: <HA system-user token>}` (`music_assistant/controllers/discovery/controller.py:_announce_to_homeassistant`) | `http://host:port` directly; core aborts on `InvalidServerVersion` (`core/components/music_assistant/config_flow.py:138`) | **Bearer token from the discovery config.** The ingress site is accepted only by socket-level check of 172.30.32.x (`webserver/helpers/auth_middleware.py:is_request_from_ingress`) |
| **ESPHome** (`esphome/home-assistant-addon`, `host_network: true`) | `[esphome, mcp]` | `{host: 127.0.0.1, port: <ingress port>}` (`esphome/docker/ha-addon-rootfs/.../discovery/run`) | Dashboard API on the ingress port, reachable because of host networking; `async_step_hassio` just stores dashboard info and aborts (`core/components/esphome/config_flow.py:506`) | None |
| **Byonk** (same-repo, custom) | `[byonk]` | discovery triggers the flow | `http://{addon_info.hostname}:{port}` from HA's `hassio` `AddonManager` (`custom_components/byonk/addon.py:async_get_base_url`) | Integration generates a token, writes it into the add-on's **options**, restarts the add-on, and reads it back (`addon.py:async_provision_token`, `async_read_token`) |

### Options for ha-spark

- **A. Discovery with a token in the config (the Music Assistant pattern).
  Recommended.** On start, the add-on generates (or loads from `/data`) a random
  integration token. It posts `{host: $HOSTNAME, port: <api port>,
  auth_token: <token>}` to `/discovery`. The integration stores the token in
  its config entry and sends it as a bearer. This reuses the existing
  `auth.verify` bearer gate in `server.py`. The token never appears in
  `options.json` and never passes through the user. Only Core can read it from
  the Supervisor.
- **B. Integration provisions the token into add-on options (the Byonk
  pattern).** This works, but it puts the token in `/data/options.json` and in
  the Supervisor UI, and it needs an add-on restart. It also depends on
  `homeassistant.components.hassio.AddonManager`, an HA-internal helper with
  no stability promise for custom integrations.
- **C. A hard-coded internal hostname.** Hostnames are `{REPO}_{SLUG}` with `_`
  replaced by `-`, where `{REPO}` is a hash of the repo URL
  ([add-on communication](https://developers.home-assistant.io/docs/add-ons/communication/)).
  The hash is install-specific, so hard-coding it is fragile. Discovery (or
  `addon_info.hostname`) gives it at runtime instead.
- **D. Ingress.** Ingress is a user-session proxy. "Users are previously
  authenticated via Home Assistant" and "Only connections from `172.30.32.2`
  must be allowed"
  ([add-on presentation](https://developers.home-assistant.io/docs/add-ons/presentation/)).
  No pair above sends integration traffic through it, and a Core integration
  has no user session to present. Use ingress for the web UI only.

On non-Supervised installs (Container or Core) there is no add-on and no
discovery. The integration still needs a `user` step and has to run without
the add-on, which matches map #247's "warns but still works" decision.

### Security finding (flag only, not fixed here)

`ha_spark/energy/scheduler.py:873` binds the ingress app on `0.0.0.0:8099`
with no auth. The rationale in `ha_spark/api/server.py` is that "the only route
in is HA's ingress proxy". But port 8099 is also reachable from other
containers on the Supervisor's internal network, at the add-on's hostname, and
HA's docs say an ingress server should deny every source except `172.30.32.2`.
`POST /api/config` is a mutation on that port. Before the integration ships,
the API should either:

- (a) accept unauthenticated requests only from `172.30.32.2` (ingress) and
  require the discovery-issued bearer token from everything else, or
- (b) serve the integration on a separate internal listener that always
  requires the token, as Music Assistant does with its separate ingress and
  API sites.

## 4. Releases and tags: how the two coexist

Current add-on rules (`docs/releasing.md`, `ha_spark_addon/Dockerfile`):

- The store reads `version` from `config.yaml` on **`master`**.
- The image `pip install`s `git+…@v${BUILD_VERSION}`, so the `vX.Y.Z` tag must
  exist.

HACS adds one rule: it reads the **latest GitHub Release**.

A single shared tag satisfies both, because the add-on build and HACS both
resolve `vX.Y.Z`. Proposed sequence:

1. Bump `config.yaml` `version`, `custom_components/ha_spark/manifest.json`
   `version` and `pyproject.toml` to the same `X.Y.Z`. Add a test that fails if
   they differ, as Byonk does in its release workflow.
2. Tag `vX.Y.Z` and push the branch and the tag.
3. Merge to `master`. The store now advertises `X.Y.Z`.
4. **Then** publish the GitHub Release for `vX.Y.Z`. Only now does HACS offer
   it. Publishing the Release last keeps HACS from offering an integration
   whose add-on isn't in the store yet. Use GitHub "pre-release" for RCs: HACS
   shows those only to users who opt into beta.

Set `hacs.json` `hide_default_branch: true`, so HACS users can't install
`master` between releases.

## 5. Keeping the two compatible at runtime

Lockstep versions don't guarantee lockstep installs. Supervisor and HACS
update independently, and a custom integration only loads on an HA restart.
Established practice is a runtime handshake:

- Music Assistant's flow aborts with `invalid_server_version` when the server's
  schema version isn't supported (`core/components/music_assistant/config_flow.py`).
- Byonk raises a **repair issue** on skew instead of blocking (Byonk spec,
  decision 4).

Recommendation for ha-spark:

- Expose an `api_version` integer, separate from the release version, on an
  unauthenticated-safe `GET /api/info`.
- The integration declares the range it supports. Out of range, it raises a
  repair issue and keeps its entities read-only, rather than refusing to load.
  That fits map #247: the integration warns but still works.
- Bump `api_version` only on breaking contract changes, so patch releases of
  either half stay compatible.

## 6. Alternative worth weighing (Clause 5)

Byonk's later design drops HACS: the add-on writes the integration into HA's
config dir itself (`homeassistant_config:rw` map), then shows a persistent
notification asking for a restart. The tradeoffs:

- **Pro:** users install one thing, there's no HACS dependency and no
  default-store queue, and version skew is reduced because the add-on ships its
  matching integration.
- **Con:** the add-on gets read/write access to HA's whole config dir,
  including `secrets.yaml`. That's a large privilege increase for an add-on
  that already holds credentials and actuates hardware, and it cuts against
  CLAUDE.md's security posture.
- **Con:** files written outside HACS are invisible to HACS. A user who also
  has a HACS copy gets conflicts.
- **Con:** an HA restart is still needed, and HACS's update UX is lost.

Recommendation: ship through HACS (custom repository first, default store
later). Revisit add-on-installs-integration only if the HACS UX proves to be a
real onboarding blocker for new households.

## Sources

- HACS: [publish/start](https://hacs.xyz/docs/publish/start/), [publish/integration](https://hacs.xyz/docs/publish/integration/), [publish/action](https://hacs.xyz/docs/publish/action/), [publish/include](https://hacs.xyz/docs/publish/include/)
- HA developer docs: [add-on configuration](https://developers.home-assistant.io/docs/add-ons/configuration/), [add-on communication](https://developers.home-assistant.io/docs/add-ons/communication/), [add-on presentation (ingress)](https://developers.home-assistant.io/docs/add-ons/presentation/), [config flow handlers](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/), [brand images](https://developers.home-assistant.io/docs/core/integration/brand_images), [2026-02-24 brands proxy blog](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)
- Supervisor @`9ce1060b`: [`api/discovery.py`](https://github.com/home-assistant/supervisor/blob/9ce1060ba7cfb833899d0ba81d8dbaf9fa4eed15/supervisor/api/discovery.py), [`discovery/__init__.py`](https://github.com/home-assistant/supervisor/blob/9ce1060ba7cfb833899d0ba81d8dbaf9fa4eed15/supervisor/discovery/__init__.py), [`discovery/validate.py`](https://github.com/home-assistant/supervisor/blob/9ce1060ba7cfb833899d0ba81d8dbaf9fa4eed15/supervisor/discovery/validate.py), [`store/data.py`](https://github.com/home-assistant/supervisor/blob/9ce1060ba7cfb833899d0ba81d8dbaf9fa4eed15/supervisor/store/data.py), [`api/middleware/security.py`](https://github.com/home-assistant/supervisor/blob/9ce1060ba7cfb833899d0ba81d8dbaf9fa4eed15/supervisor/api/middleware/security.py)
- HA Core @`f6f55350`: [`components/hassio/discovery.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/components/hassio/discovery.py), [`helpers/discovery_flow.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/helpers/discovery_flow.py), [`helpers/service_info/hassio.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/helpers/service_info/hassio.py), [`zwave_js/config_flow.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/components/zwave_js/config_flow.py), [`music_assistant/config_flow.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/components/music_assistant/config_flow.py), [`esphome/config_flow.py`](https://github.com/home-assistant/core/blob/f6f553509475a1cf14f53d22f9ab8ca2ed72c8e8/homeassistant/components/esphome/config_flow.py)
- Z-Wave JS add-on @`014df506`: [`zwave_js/config.yaml`](https://github.com/home-assistant/addons/blob/014df506f8e47567b92226db0ac152048286d5c3/zwave_js/config.yaml), [`discovery`](https://github.com/home-assistant/addons/blob/014df506f8e47567b92226db0ac152048286d5c3/zwave_js/rootfs/etc/services.d/zwave_js/discovery)
- Music Assistant: [add-on `config.yaml` @`2a1187bd`](https://github.com/music-assistant/home-assistant-addon/blob/2a1187bd596256d5537fce4c83f6ba6e87b1799e/music_assistant/config.yaml), [server `discovery/controller.py` @`7e367084`](https://github.com/music-assistant/server/blob/7e3670848bb9edb6dac3f636672758ba3e9b8878/music_assistant/controllers/discovery/controller.py), [`auth_middleware.py`](https://github.com/music-assistant/server/blob/7e3670848bb9edb6dac3f636672758ba3e9b8878/music_assistant/controllers/webserver/helpers/auth_middleware.py)
- ESPHome: [add-on `config.yaml` @`2df5fa4e`](https://github.com/esphome/home-assistant-addon/blob/2df5fa4e1ba32cb60ed949776227c9cee8d3dce4/esphome/config.yaml), [`discovery/run` @`3c634ecf`](https://github.com/esphome/esphome/blob/3c634ecfd8de35dbd930246344ed6fa1fd586a0f/docker/ha-addon-rootfs/etc/s6-overlay/s6-rc.d/discovery/run)
- Byonk @`133c5286`: [repo root](https://github.com/oetiker/byonk/tree/133c528613952539f5216b6db7983405dcc627c6), [`custom_components/byonk/addon.py`](https://github.com/oetiker/byonk/blob/133c528613952539f5216b6db7983405dcc627c6/custom_components/byonk/addon.py), [add-on-installs-integration spec](https://github.com/oetiker/byonk/blob/133c528613952539f5216b6db7983405dcc627c6/docs/superpowers/specs/2026-08-19-addon-installs-integration-design.md)
- This repo: `repository.yaml`, `ha_spark_addon/config.yaml`, `ha_spark_addon/Dockerfile`, `ha_spark/api/server.py`, `ha_spark/energy/scheduler.py:873`, `docs/releasing.md`
