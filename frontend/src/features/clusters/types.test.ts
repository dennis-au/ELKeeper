import { describe, expect, it } from 'vitest';
import type { PortProfile } from './types';

describe('cluster feature types', () => {
  it('keeps the public port profile shape', () => {
    const ports: PortProfile = { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 };
    expect(ports.elasticsearch_transport).toBe(9300);
  });
});
