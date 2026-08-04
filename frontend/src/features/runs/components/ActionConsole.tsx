import { useEffect, useMemo, useRef, useState } from 'react';
import { EuiBadge, EuiButtonIcon, EuiFlexGroup, EuiFlexItem, EuiText } from '@elastic/eui';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { runsApi, watchRun } from '../api';
import type { RunRecord } from '../types';

interface Props {
  collapseKey?: string;
  watchedRunId?: number;
  onWatch: (runId?: number) => void;
}

export function ActionConsole({ collapseKey, watchedRunId, onWatch }: Props) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<number>();
  const [terminalReady, setTerminalReady] = useState(false);
  const [log, setLog] = useState('');
  const terminalHost = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal>();
  const fit = useRef<FitAddon>();
  const source = useRef<EventSource>();
  const previousCollapseKey = useRef(collapseKey);
  const { data: runs = [] } = useQuery({ queryKey: ['runs'], queryFn: runsApi.list, refetchInterval: 3000 });
  const activeRuns = useMemo(() => runs.filter((run) => ['queued', 'running'].includes(run.status)).slice(0, 5), [runs]);
  const selected = runs.find((run) => run.id === selectedId) || runs.find((run) => run.id === watchedRunId) || runs[0];

  useEffect(() => {
    if (watchedRunId) {
      setSelectedId(watchedRunId);
      setOpen(true);
    }
  }, [watchedRunId]);

  useEffect(() => {
    if (collapseKey === previousCollapseKey.current) return;
    previousCollapseKey.current = collapseKey;
    setOpen(false);
  }, [collapseKey]);

  useEffect(() => {
    if (!open || !terminalHost.current) return;
    const instance = new Terminal({
      convertEol: true,
      cursorBlink: false,
      disableStdin: true,
      fontFamily: 'SFMono-Regular, Consolas, Liberation Mono, monospace',
      fontSize: 12,
      lineHeight: 1.35,
      scrollback: 8000,
      theme: { background: '#0b0f14', foreground: '#d7dee7', cursor: '#d7dee7', selectionBackground: '#31516f' },
    });
    const addon = new FitAddon();
    terminal.current = instance;
    fit.current = addon;
    instance.loadAddon(addon);
    instance.open(terminalHost.current);
    addon.fit();
    setTerminalReady(true);
    const observer = new ResizeObserver(() => addon.fit());
    observer.observe(terminalHost.current);
    const frame = requestAnimationFrame(() => addon.fit());
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      instance.dispose();
      if (terminal.current === instance) terminal.current = undefined;
      if (fit.current === addon) fit.current = undefined;
      setTerminalReady(false);
    };
  }, [open]);

  useEffect(() => {
    setLog(selected?.log || '');
  }, [selected?.id, selected?.log]);

  useEffect(() => {
    if (!selected) return;
    source.current?.close();
    const close = watchRun(selected.id, selected.events_token, {
      onLog: (value) => setLog(value),
      onCompleted: () => {
      if (selected.id === watchedRunId) {
        void Promise.all([
          queryClient.invalidateQueries({ queryKey: ['nodes'] }),
          queryClient.invalidateQueries({ queryKey: ['dashboard'] }),
          queryClient.invalidateQueries({ queryKey: ['clusters'] }),
          queryClient.invalidateQueries({ queryKey: ['runs'] }),
        ]);
      }
      },
    });
    return close;
  }, [queryClient, selected?.id, watchedRunId]);

  useEffect(() => {
    const instance = terminal.current;
    if (!open || !terminalReady || !instance) return;
    instance.reset();
    instance.write((log || 'Waiting for output...').replaceAll('\n', '\r\n'), () => {
      fit.current?.fit();
      instance.scrollToBottom();
    });
  }, [log, open, terminalReady]);

  const tabs: RunRecord[] = activeRuns.length ? activeRuns : runs.slice(0, 4);
  return (
    <section className={`action-console ${open ? 'is-open' : ''}`} aria-label="Live action log">
      <div className="action-console__bar">
        <EuiFlexGroup alignItems="center" gutterSize="s" responsive={false}>
          <EuiFlexItem grow={false}>
            <EuiButtonIcon iconType={open ? 'arrowDown' : 'arrowUp'} aria-label={open ? 'Collapse action log' : 'Expand action log'} onClick={() => setOpen((value) => !value)} />
          </EuiFlexItem>
          <EuiFlexItem grow={false}><EuiText size="s"><strong>Live actions</strong></EuiText></EuiFlexItem>
          {tabs.map((run) => (
            <EuiFlexItem grow={false} key={run.id}>
              <button className={`run-tab ${selected?.id === run.id ? 'is-selected' : ''}`} onClick={() => { setSelectedId(run.id); onWatch(run.id); setOpen(true); }}>
                #{run.id} {run.kind} <EuiBadge color={run.status === 'failed' ? 'danger' : run.status === 'succeeded' ? 'success' : 'primary'}>{run.status}</EuiBadge>
              </button>
            </EuiFlexItem>
          ))}
          <EuiFlexItem />
          <EuiFlexItem grow={false}><EuiText size="xs" color="subdued">{selected ? selected.target : 'No actions yet'}</EuiText></EuiFlexItem>
        </EuiFlexGroup>
      </div>
      {open && <div ref={terminalHost} className="action-console__terminal" />}
    </section>
  );
}
