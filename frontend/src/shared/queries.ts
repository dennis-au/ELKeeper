import { advancedApi } from '../features/advanced';
import { clusterApi } from '../features/clusters';
import { dashboardApi } from '../features/dashboard';
import { hostApi } from '../features/hosts';
import { maintenanceApi } from '../features/maintenance';
import { runsApi } from '../features/runs';

/** Compatibility query catalog used by pages that have not yet moved to a feature hook. */
export const queries = {
  clusters: clusterApi.list,
  nodes: hostApi.list,
  runs: runsApi.list,
  dashboard: dashboardApi.snapshot,
  controllerSettings: advancedApi.controllerSettings,
  maintenanceCapabilities: maintenanceApi.capabilities,
};
