import { createContext, useContext } from 'react';
import type { Cluster } from './features/clusters';

export type NavigationGuard = (continueNavigation: () => void) => boolean;

export interface ConsoleContextValue {
  clusters: Cluster[];
  selectedCluster?: Cluster;
  selectedClusterId?: number;
  setSelectedClusterId: (id: number) => void;
  watchRun: (runId: number) => void;
  refreshAll: () => Promise<void>;
  registerNavigationGuard?: (guard?: NavigationGuard) => void;
}

export const ConsoleContext = createContext<ConsoleContextValue | null>(null);

export function useConsole() {
  const value = useContext(ConsoleContext);
  if (!value) throw new Error('Console context is unavailable');
  return value;
}
