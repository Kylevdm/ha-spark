# The integration's identity is a generated site id, never the discovery uuid

Status: accepted (2026-10-15), confirmed by the owner in [Integration devices, identity and config entries](https://github.com/Kylevdm/ha-spark/issues/321). This is a v1 planning contract, not implemented behavior.

The companion integration has exactly one config entry per site (`single_config_entry`). Its `unique_id` is a site uuid generated once when the entry is created, whatever the source (Supervisor discovery or standalone adoption). It is never the Supervisor discovery uuid. Every identity the integration cannot read from hardware (the site, each bus or gateway, each battery bank) is derived from that site uuid plus a generated, stored suffix. Inverters use their hardware serial.

The obvious choice, keying a discovery-created entry on the discovery uuid, is a trap. When an add-on is uninstalled, Home Assistant deletes every `hassio`-source entry whose `unique_id` equals the discovery uuid (Core `hassio/discovery.py:142-160`). That would delete the integration's entry and its authority store, which breaks integration-owned authority ([ADR-0005](0005-integration-authority-and-operating-modes.md)) and the removal handover ([ADR-0008](0008-operating-mode-and-authority-lifecycle.md)). We also rejected one entry per bus, because the site is the unit of authority (one mode, one epoch, one store), and per-bus entries would split that store.

## Amendment (2026-10-24): flow-enforced single entry

One entry per site is enforced by the config flow, not the manifest's
`single_config_entry` flag. Home Assistant applies that flag before the hassio
step runs, which would block the per-boot discovery refresh. The linked add-on's
refresh updates connection data silently; other flows cannot create a second
site. The generated site identity and the intent above are unchanged.

## Consequences

- Discovery only links the add-on. For an entry that already links this add-on, the per-boot discovery refreshes host, port and token silently. Switching an entry adopted standalone over to a discovered add-on needs an HA admin's confirmation, because it changes who may command the hardware.
- Changing a `unique_id` later would orphan the device and entity registry and its statistics, so these formats are effectively permanent: `<site_uuid>`, `solis_<serial>`, `alphaess_<EMS SN>`, `<site_uuid>_bus_<id>`, `<site_uuid>_bank_<id>`, and entities `<device id>_<fixed key>`.
- An inverter whose serial cannot be read is not adopted, and a changed serial at a known address blocks writes until the owner confirms the replacement. A generated fallback id would silently defeat that check.
