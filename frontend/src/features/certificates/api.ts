import { api, jsonBody } from '../../shared/api';
import type {
  CertificateInventoryResponse, CertificateOperation, CertificatePolicy,
  CertificatePreview, CertificateTrustConsumer,
} from './types';

export const certificatesApi = {
  inventory: (clusterId: number) => api<CertificateInventoryResponse>(`/api/clusters/${clusterId}/certificates`),
  policy: (clusterId: number) => api<CertificatePolicy>(`/api/clusters/${clusterId}/certificate-policy`),
  updatePolicy: (clusterId: number, value: Partial<CertificatePolicy> & { expected_revision: number }) => api<CertificatePolicy>(`/api/clusters/${clusterId}/certificate-policy`, { method: 'PUT', ...jsonBody(value) }),
  consumers: (clusterId: number) => api<{ items: CertificateTrustConsumer[]; retirement_blocked: boolean; blockers: string[] }>(`/api/clusters/${clusterId}/certificate-trust-consumers`),
  operations: (clusterId: number) => api<{ items: CertificateOperation[] }>(`/api/clusters/${clusterId}/certificate-operations`),
  refresh: (clusterId: number) => api<{ run_id?: number | null }>(`/api/clusters/${clusterId}/certificates/refresh`, { method: 'POST' }),
  renewalPreview: (certificateId: string) => api<CertificatePreview>(`/api/certificates/${encodeURIComponent(certificateId)}/renewal-preview`, { method: 'POST' }),
  caRotationPreview: (clusterId: number) => api<CertificatePreview>(`/api/clusters/${clusterId}/ca-rotation-preview`, { method: 'POST' }),
};
