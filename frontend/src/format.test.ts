import { describe, expect, it } from 'vitest';
import { bytes, formatDateTime, formatTime, percent, roleLabel } from './format';

describe('format helpers', () => {
  it('formats operational resource values', () => {
    expect(bytes(4 * 1024 ** 3)).toBe('4.0 GiB');
    expect(percent(3, 4)).toBe(75);
  });

  it('uses human-readable Elastic role labels', () => {
    expect(roleLabel('fleet-server')).toBe('Fleet Server');
    expect(roleLabel('custom-role')).toBe('custom-role');
  });

  it('formats UTC timestamps in the configured display timezone', () => {
    const timestamp = '2026-08-01T11:06:37.650745Z';
    expect(formatDateTime(timestamp, 'Asia/Hong_Kong')).not.toContain('T11:06:37');
    expect(formatTime(timestamp, 'Asia/Hong_Kong')).not.toBe('');
  });
});
