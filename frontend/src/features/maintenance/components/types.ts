export type MaintenancePlanState =
  | 'draft'
  | 'ready'
  | 'blocked'
  | 'executing'
  | 'paused'
  | 'recovery_required'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export type MaintenanceFreshnessState = 'fresh' | 'stale' | 'expired';
export type MaintenancePredicateOutcome = 'passed' | 'warning' | 'blocking';
export type MaintenanceTargetKind = 'host' | 'cluster' | 'assignment';
export type MaintenanceAvailability = 'preserved' | 'degraded' | 'unavailable';

export type MaintenanceStepState =
  | 'pending'
  | 'active'
  | 'completed'
  | 'blocked'
  | 'failed'
  | 'paused'
  | 'recovery_required'
  | 'skipped';

export type MaintenanceAction = 'execute' | 'pause' | 'resume' | 'cancel' | 'recover';

export interface MaintenanceFreshness {
  state: MaintenanceFreshnessState;
  observedAt?: string;
  expiresAt?: string;
  detail?: string;
}

export interface MaintenancePlanHeaderData {
  planId: string;
  state: MaintenancePlanState;
  target: {
    kind: MaintenanceTargetKind;
    name: string;
  };
  operation: string;
  reason: string;
  requester: string;
  createdAt: string;
  freshness: MaintenanceFreshness;
  policy: {
    name: string;
    revision: number;
    availabilityMode: string;
  };
}

export interface MaintenanceImpactWorkload {
  id: string | number;
  name: string;
  role: string;
  host: string;
  availability: MaintenanceAvailability;
}

export interface MaintenanceImpactEndpoint {
  id: string | number;
  name: string;
  availability: MaintenanceAvailability;
  detail?: string;
}

export interface MaintenanceDataTierImpact {
  tier: string;
  availableAfter: number;
  total: number;
  minimumRequired: number;
  safe: boolean;
}

export interface MaintenanceImpact {
  clusters: Array<{ id: string | number; name: string }>;
  workloads: MaintenanceImpactWorkload[];
  endpoints: MaintenanceImpactEndpoint[];
  masterQuorum?: {
    availableAfter: number;
    total: number;
    required: number;
    preserved: boolean;
  };
  dataTiers: MaintenanceDataTierImpact[];
  agents: {
    affected: number;
    interruptionExpected: boolean;
  };
  singletonServices?: Array<{
    name: string;
    estimatedOutage?: string;
  }>;
}

export interface MaintenancePredicateResult {
  id: string;
  title: string;
  outcome: MaintenancePredicateOutcome;
  evidence: string;
  remediation?: string;
  observedAt?: string;
  forceable?: boolean;
}

export interface MaintenancePlanStep {
  id: string;
  sequence: number;
  title: string;
  description: string;
  state: MaintenanceStepState;
  target?: string;
  checkpoint?: {
    label: string;
    verifiedAt?: string;
  };
}

export interface MaintenancePlanViewModel {
  header: MaintenancePlanHeaderData;
  impact: MaintenanceImpact;
  predicates: MaintenancePredicateResult[];
  steps: MaintenancePlanStep[];
  statusDetail?: string;
  lastVerifiedCheckpoint?: string;
}

export interface MaintenanceActionControl {
  visible?: boolean;
  enabled: boolean;
  reason?: string;
  label?: string;
}

export type MaintenanceActionControls = Partial<Record<MaintenanceAction, MaintenanceActionControl>>;
export type MaintenanceTimestampFormatter = (value?: string) => string;
