import { fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom';
import { Shell } from './Shell';

vi.mock('./ActionConsole', () => ({ ActionConsole: () => null }));

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/dashboard" element={<div>Dashboard content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Shell navigation', () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify([]), { status: 200 })));
  });

  it('collapses and restores desktop navigation from the header control', async () => {
    renderShell();

    const toggle = await screen.findByRole('button', { name: 'Hide navigation' });
    expect(document.querySelector('.app-shell')).toHaveClass('nav-is-open');

    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Open navigation' })).toBeInTheDocument();
    expect(document.querySelector('.app-shell')).toHaveClass('nav-is-closed');

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation' }));
    expect(screen.getByRole('button', { name: 'Hide navigation' })).toBeInTheDocument();
    expect(document.querySelector('.app-shell')).toHaveClass('nav-is-open');
  });
});
