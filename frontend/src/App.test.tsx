import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import App from './App';
import { ConsoleContext } from './app-context';
import { AdvancedPage } from './pages/AdvancedPage';
import { RolesPage } from './pages/RolesPage';

describe('console entry', () => {
  beforeEach(() => sessionStorage.clear());

  it('shows the in-page sign-in form without browser dialogs', () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/']}><App /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByRole('img', { name: 'ELKeeper logo' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByLabelText('Username')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled();
  });

  it('contains no native alert, confirm, or prompt interactions', () => {
    const modules = import.meta.glob('./**/*.tsx', { eager: true, query: '?raw', import: 'default' }) as Record<string, string>;
    const source = Object.values(modules).join('\n');
    expect(source).not.toMatch(/\b(?:alert|confirm|prompt)\s*\(/);
    expect(modules['./components/Shell.tsx']).toContain('mobileBreakpoints={[]}');
    expect(modules['./components/Shell.tsx']).not.toContain('sidebar-clusters');
    expect(source).not.toContain('iInCircle');
  });

  it('keeps cluster-scoped page headings visible in empty states', () => {
    const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } });
    const context = {
      clusters: [],
      selectedCluster: undefined,
      selectedClusterId: undefined,
      setSelectedClusterId: () => undefined,
      watchRun: () => undefined,
      refreshAll: async () => undefined,
    };
    const roles = render(<QueryClientProvider client={client}><ConsoleContext.Provider value={context}><RolesPage /></ConsoleContext.Provider></QueryClientProvider>);
    expect(screen.getByRole('heading', { name: 'Role Assignment' })).toBeInTheDocument();
    roles.unmount();
    render(<QueryClientProvider client={client}><ConsoleContext.Provider value={context}><AdvancedPage /></ConsoleContext.Provider></QueryClientProvider>);
    expect(screen.getByRole('heading', { name: 'Advance' })).toBeInTheDocument();
  });
});
