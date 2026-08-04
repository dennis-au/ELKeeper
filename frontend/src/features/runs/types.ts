/** Public immutable run-record contract used by action-console consumers. */
export interface RunRecord {
  id: number;
  kind: string;
  target: string;
  status: string;
  log: string;
  created_at: string;
  finished_at?: string;
  events_token: string;
}
