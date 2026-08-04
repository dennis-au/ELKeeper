import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { ControllerIdentityPanel } from './ControllerIdentityPanel';

vi.mock('../../../shared/api', () => ({
  api: vi.fn().mockResolvedValue({
    managed: false,
    active: {
      key_id: 'SHA256:legacy-fingerprint', algorithm: 'ed25519', public_key: 'ssh-ed25519 AAAAlegacy',
      source: 'legacy_mounted', state: 'legacy', created_at: null,
    },
    candidate: null,
  }),
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
}));

describe('ControllerIdentityPanel', () => {
  it('shows the current key identifier without exposing private material', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><ControllerIdentityPanel /></QueryClientProvider>);
    expect(await screen.findByText('SHA256:legacy-fingerprint')).toBeInTheDocument();
    expect(screen.getByText('Legacy mounted key')).toBeInTheDocument();
    expect(screen.queryByText(/private key material/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate key' })).not.toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Timezone' })).toBeInTheDocument();
  });
});
