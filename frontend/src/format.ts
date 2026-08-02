export function bytes(value = 0) {
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

export function percent(value = 0, total = 0) {
  return total > 0 ? Math.round((value / total) * 100) : 0;
}

export function dateValue(value?: string) {
  if (!value) return undefined;
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value) ? `${value.replace(' ', 'T')}Z` : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

export function formatDateTime(value?: string, timezone = 'UTC') {
  const date = dateValue(value);
  if (!date) return 'Never';
  return new Intl.DateTimeFormat(undefined, {
    timeZone: timezone,
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  }).format(date);
}

export function formatTime(value?: string, timezone = 'UTC') {
  const date = dateValue(value);
  if (!date) return '';
  return new Intl.DateTimeFormat(undefined, { timeZone: timezone, hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date);
}

export function timeAgo(value?: string) {
  if (!value) return 'Never';
  const date = dateValue(value);
  if (!date) return 'Never';
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

export function roleLabel(role: string) {
  return ({
    master: 'Master', hot: 'Hot data', warm: 'Warm data', ml: 'Machine learning', ingest: 'Ingest',
    coordinating: 'Coordinating', kibana: 'Kibana', 'fleet-server': 'Fleet Server', logstash: 'Logstash', 'elastic-agent': 'Elastic Agent',
  } as Record<string, string>)[role] || role;
}
