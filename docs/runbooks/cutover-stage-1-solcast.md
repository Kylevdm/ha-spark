# Runbook: cutover stage 1, native Solcast

This runbook moves the reference household's solar forecast from the BJReplay
Solcast integration to ha-spark's own Solcast client. It makes no hardware
writes: the site stays in Observe throughout, and the stage is accepted only
when Home Assistant repairs and Energy dashboard validation are clean.

You need the ha-spark add-on and companion integration on 0.29.0 or later, a
Solcast API key, and the BJReplay Solcast integration still installed. Keep
BJReplay installed until this stage is accepted.

## Cutover

1. **Enter Observe.** Use the ingress mode page or `ha-spark mode set observe`.
   Wait for the handover to complete, then confirm `ha-spark mode show` reports
   Observe. Check that no hardware write is made.
2. **Inventory forecast references.** In Home Assistant, use the Related/search
   panel and search `.storage/lovelace*`, `automations.yaml` and `scripts.yaml`
   for every reference to the BJReplay forecast entities. Record each dashboard,
   automation, script and template that reads today's forecast, tomorrow's
   forecast or their attributes. Check the inventory is complete before
   changing anything.
3. **Stop BJReplay polling.** Turn off the BJReplay integration's automatic
   update, keeping the integration installed and its history intact. Check that
   no other Solcast poller is still scheduled. Two pollers would share the same
   daily quota.
4. **Enter the Solcast key.** Open **Settings → Devices & services**, select
   **ha-spark**, then **Reconfigure**. Enter the key and let the flow find the
   rooftop sites. Set the daily call limit in the integration's **Options** flow
   if this is a legacy 50-call account; otherwise leave it at 10. Check the
   **Solcast** device appears and a first fetch succeeds. Run `ha-spark health`
   and confirm the Integration link line ends with
   `solar: native; ok; fetch age Ns`.
5. **Clear the legacy forecast settings.** Clear `solar_today_entity` and
   `solar_tomorrow_entity` in ha-spark's settings. Check `ha-spark config show`
   reports both empty and `ha-spark health` no longer requires the old forecast
   entity on this native site.
6. **Repoint references in the same window.** Update the inventory from step 2
   to the new Solcast sensors, attributes or Energy forecast. Do not add template
   shims: point each reference at the native source directly. Check a repeat
   search finds no remaining BJReplay forecast references.
7. **Re-select the Energy solar forecast.** Open the Energy dashboard settings
   and select ha-spark's forecast for solar production. Check only that forecast
   is selected, so the dashboard is not counting two sources.
8. **Accept only when clean.** Open **Settings → System → Repairs** and confirm
   no ha-spark or Solcast repair is present. Confirm the Energy dashboard shows
   no validation error for the selected forecast and that its values come from
   the new source. Check the plan view's solar forecast matches the native
   source. Do not accept while either check is dirty.
9. **Settle in Observe for a few days.** Watch the daily forecasts, the *Last
   fetch* and *API calls used* sensors, and the plan's solar figures. Check the
   quota counter resets at midnight UTC and that a restart reuses the cache
   without polling. Resolve any repair or Energy validation issue before
   continuing.
10. **Re-enter Protect, then Optimise.** First confirm a fresh plan built from
    the native forecast is fully verified in Observe. Then request Protect and
    verify the handover. With the fresh plan still verified, request Optimise;
    it stays activating until its first fresh plan is applied and read back.
    Check the committed mode and the plan view after each handover.

## Rollback

Stay in Observe. If the site has already left Observe, request Observe first and
verify the handover completes. Then:

1. Remove native Solcast with **Settings → Devices & services → ha-spark →
   Reconfigure**, leaving the key empty and confirming removal.
2. Re-enable BJReplay automatic polling.
3. Restore `solar_today_entity` and `solar_tomorrow_entity` to their previous
   values and confirm the plan uses them again.
4. If references were already repointed, restore them in the same window and
   re-select BJReplay's forecast in the Energy dashboard.

BJReplay was kept installed, so rollback needs no reinstall. Do not re-enter
Protect or Optimise until the restored legacy forecast is visible and the site
is clean.
