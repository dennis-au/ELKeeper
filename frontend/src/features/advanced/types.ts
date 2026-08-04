/** Public controller identity and sensitive-item contracts. */
export interface ControllerSshKey {
  key_id: string;
  algorithm: string;
  public_key: string;
  source: 'generated' | 'imported' | 'legacy_mounted';
  state: 'active' | 'candidate' | 'legacy';
  created_at?: string | null;
}

export interface ControllerSshKeyStatus {
  active: ControllerSshKey;
  candidate?: ControllerSshKey | null;
  managed: boolean;
}

export interface ControllerSettings {
  timezone: string;
}

export interface SensitiveItem {
  id: string;
  label: string;
  category: string;
  source: string;
  available: boolean;
  masked_value: string;
  fingerprint?: string;
  expires_at?: string;
  storage_path?: string;
}
