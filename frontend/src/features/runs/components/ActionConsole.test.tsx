import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActionConsole } from './ActionConsole';

const terminalState = vi.hoisted(() => ({ instances: [] as Array<{ write: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn> }> }));
const eventSourceState = vi.hoisted(() => ({ instances: [] as EventSourceMock[] }));

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    write = vi.fn((_: string, callback?: () => void) => callback?.());
    reset = vi.fn();
    loadAddon = vi.fn();
    open = vi.fn();
    scrollToBottom = vi.fn();
    dispose = vi.fn();
    constructor() { terminalState.instances.push(this); }
  },
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class { fit = vi.fn(); },
}));

class EventSourceMock {
  listeners = new Map<string, (event: Event) => void>();
  addEventListener = vi.fn();
  close = vi.fn();

  constructor() {
    eventSourceState.instances.push(this);
    this.addEventListener.mockImplementation((event, listener) => this.listeners.set(event, listener));
  }

  emit(event: string) {
    this.listeners.get(event)?.(new Event(event));
  }
}

describe('ActionConsole', () => {
  afterEach(cleanup);

  beforeEach(() => {
    terminalState.instances.length = 0;
    eventSourceState.instances.length = 0;
    vi.stubGlobal('EventSource', EventSourceMock);
  });

  it('hydrates the visible terminal again after collapsing and reopening', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(['runs'], [{
      id: 77,
      kind: 'reconcile',
      target: 'test:node1:master',
      status: 'running',
      log: 'first line\nsecond line\n',
      created_at: '2026-08-01T00:00:00Z',
      events_token: 'event-token',
    }]);
    render(
      <QueryClientProvider client={client}>
        <ActionConsole watchedRunId={77} onWatch={() => undefined} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(terminalState.instances).toHaveLength(1));
    expect(terminalState.instances[0].write).toHaveBeenCalledWith('first line\r\nsecond line\r\n', expect.any(Function));

    fireEvent.click(screen.getByRole('button', { name: 'Collapse action log' }));
    await waitFor(() => expect(terminalState.instances[0].dispose).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole('button', { name: 'Expand action log' }));
    await waitFor(() => expect(terminalState.instances).toHaveLength(2));
    expect(terminalState.instances[1].write).toHaveBeenCalledWith('first line\r\nsecond line\r\n', expect.any(Function));
  });

  it('collapses when the active page changes', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(['runs'], [{
      id: 77,
      kind: 'reconcile',
      target: 'test:node1:master',
      status: 'running',
      log: 'deployment output\n',
      created_at: '2026-08-01T00:00:00Z',
      events_token: 'event-token',
    }]);
    const view = render(
      <QueryClientProvider client={client}>
        <ActionConsole collapseKey="/hosts" watchedRunId={77} onWatch={() => undefined} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: 'Collapse action log' })).toBeInTheDocument());
    view.rerender(
      <QueryClientProvider client={client}>
        <ActionConsole collapseKey="/roles" watchedRunId={77} onWatch={() => undefined} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: 'Expand action log' })).toBeInTheDocument());
    expect(terminalState.instances[0].dispose).toHaveBeenCalledOnce();
  });

  it('refreshes host data after a watched run completes', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(['runs'], [{
      id: 77,
      kind: 'host-enroll',
      target: 'node3',
      status: 'running',
      log: 'enrolling host\n',
      created_at: '2026-08-01T00:00:00Z',
      events_token: 'event-token',
    }]);
    const invalidateQueries = vi.spyOn(client, 'invalidateQueries');
    render(
      <QueryClientProvider client={client}>
        <ActionConsole watchedRunId={77} onWatch={() => undefined} />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(eventSourceState.instances).toHaveLength(1));
    eventSourceState.instances[0].emit('completed');

    await waitFor(() => {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['nodes'] });
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['dashboard'] });
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['clusters'] });
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['runs'] });
    });
  });
});
