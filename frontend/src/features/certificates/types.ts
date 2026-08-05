export interface CertificateCompatibility {
  version: string;
  supported: boolean;
  reason_code?: string | null;
  format: 'PEM';
  reload_enabled: boolean;
  restart_required: boolean;
  profile: string;
  mutation_enabled: boolean;
  mutation_blocker?: string;
}

export interface CertificateTrustDomain {
  id: string;
  cluster_id?: number;
  kind: string;
  state?: string;
  legacy_shared: boolean;
  split_migration_state: string;
  compatibility_profile?: string;
  verification_mode?: string;
  revision?: number;
}

export interface CertificateAsset {
  id: string;
  cluster_id: number;
  trust_domain_id: string;
  trust_domain: string;
  owner_type: string;
  owner_id: string;
  purpose: string;
  provider_type: string;
  management_state: string;
  storage_locator: { node_name?: string; node_id?: number; path?: string };
  desired_identity: Record<string, unknown>;
  active_generation_id?: string | null;
  health: string;
  last_observed_at?: string | null;
  legacy_shared: boolean;
  split_migration_state: string;
}

export interface CertificateInventoryResponse {
  items: CertificateAsset[];
  trust_domains: CertificateTrustDomain[];
  compatibility: CertificateCompatibility;
}

export interface CertificatePolicy {
  id: string;
  cluster_id: number;
  trust_domain_id?: string | null;
  revision: number;
  renew_before_days: number;
  critical_before_days: number;
  default_validity_days: number;
  issuer_validity_days?: number;
  offline_root_validity_days?: number;
  renewal_mode: 'manual' | 'approval_required' | 'scheduled';
  ca_retirement_observation_days?: number;
}

export interface CertificateOperation {
  id: string;
  cluster_id: number;
  operation_type: string;
  state: string;
  revision: number;
  trust_domain_ids: string[];
  request_hash: string;
  policy_revision?: number | null;
  run_id?: number | null;
  maintenance_plan_id?: string | null;
  phase: string;
  blockers: string[];
  summary: Record<string, unknown>;
  requested_by: string;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface CertificateTrustConsumer {
  id: string;
  cluster_id: number;
  trust_domain_id: string;
  trust_domain: string;
  consumer_type: 'managed' | 'external';
  consumer_kind: string;
  owner_id?: string | null;
  description: string;
  verification_method: string;
  trust_state: string;
  candidate_trust_state: string;
  last_verified_at?: string | null;
  attestation_expires_at?: string | null;
  revision: number;
  blocking_reason?: string | null;
}

export interface CertificatePreview {
  operation_id: string;
  operation_type: string;
  state: string;
  preview_hash: string;
  run_id?: number | null;
  blockers: string[];
  summary: Record<string, unknown>;
  execution_enabled: boolean;
  compatibility: CertificateCompatibility;
}
