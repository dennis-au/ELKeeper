import { api } from '../../shared/api';
import type { RunRecord } from './types';

export const runsApi = {
  list: () => api<RunRecord[]>('/api/runs'),
  eventsUrl: (runId: number, token: string) => `/api/runs/${runId}/events?token=${encodeURIComponent(token)}`,
};

export function watchRun(runId: number, token: string, handlers: {
  onLog?: (log: string) => void;
  onCompleted?: (status: string) => void;
  onError?: () => void;
}) {
  const source = new EventSource(runsApi.eventsUrl(runId, token));
  source.addEventListener('log', (event) => {
    const payload = JSON.parse((event as MessageEvent).data) as { log?: string };
    handlers.onLog?.(payload.log || '');
  });
  source.addEventListener('completed', (event) => {
    const raw = (event as MessageEvent).data;
    const payload = raw ? JSON.parse(raw) as { status?: string } : {};
    handlers.onCompleted?.(payload.status || '');
    source.close();
  });
  source.onerror = () => handlers.onError?.();
  return () => source.close();
}
