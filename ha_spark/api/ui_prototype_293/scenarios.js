/* PROTOTYPE (#293), throwaway. Named starting points for the onboarding screens:
   each replays reducer actions from initialState(start). Action vocabulary is the
   #257 reducer's (onboarding_model.js). */
window.ONBOARDING_SCENARIOS = [
  { id: 'fresh', title: 'Fresh install', start: 'fresh', actions: [] },
  {
    id: 'devices', title: 'Mid-way: devices found', start: 'fresh',
    actions: ['ADMIN_YES', 'INTEGRATION_INSTALLED', 'HA_RESTARTED', 'PAIR', 'DISCOVER_TWO'],
  },
  {
    id: 'gaps', title: 'Gaps: caching on, hold unproven', start: 'fresh',
    actions: ['ADMIN_YES', 'INTEGRATION_INSTALLED', 'HA_RESTARTED', 'PAIR', 'DISCOVER_SOLIS',
      'CACHE_ON', 'PROBE_GAP', 'LIMITS_ONE'],
  },
  {
    id: 'half', title: 'Half-saved, reloaded', start: 'fresh',
    actions: ['ADMIN_YES', 'INTEGRATION_INSTALLED', 'HA_RESTARTED', 'PAIR', 'DISCOVER_SOLIS',
      'CACHE_OFF', 'PROBE_OK', 'LIMITS_ONE', 'TARIFF_FOUND', 'SOLCAST_SKIP', 'LOADS_NONE',
      'FALLBACK_PROPOSE', 'FALLBACK_VALIDATE', 'SAVE_INTEGRATION', 'ADDON_SAVE_FAIL', 'RELOAD'],
  },
  {
    id: 'done_ready', title: 'Finished: both modes ready', start: 'fresh',
    actions: ['ADMIN_YES', 'INTEGRATION_INSTALLED', 'HA_RESTARTED', 'PAIR', 'DISCOVER_SOLIS',
      'CACHE_OFF', 'PROBE_OK', 'LIMITS_ONE', 'TARIFF_FOUND', { type: 'SOLCAST_SET', sites: 1 },
      'LOADS_EV', 'FALLBACK_PROPOSE', 'FALLBACK_VALIDATE', 'SAVE_INTEGRATION', 'SAVE_ADDON', 'FINISH'],
  },
  {
    id: 'done_gaps', title: 'Finished: no tariff, caching on', start: 'fresh',
    actions: ['ADMIN_YES', 'INTEGRATION_INSTALLED', 'HA_RESTARTED', 'PAIR', 'DISCOVER_SOLIS',
      'CACHE_ON', 'PROBE_OK', 'LIMITS_ONE', 'TARIFF_SKIP', 'SOLCAST_SKIP', 'LOADS_HEATPUMP',
      'FALLBACK_DEFER', 'SAVE_INTEGRATION', 'SAVE_ADDON', 'FINISH'],
  },
  { id: 'adopt', title: 'Adopt standalone integration', start: 'standalone', actions: [] },
  { id: 'nonadmin', title: 'Non-admin viewer', start: 'nonadmin', actions: [] },
];
