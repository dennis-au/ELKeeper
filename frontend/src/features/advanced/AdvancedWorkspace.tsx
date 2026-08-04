import { useEffect, useMemo, useRef, useState } from 'react';
import {
  EuiBadge, EuiButton, EuiButtonEmpty, EuiCallOut, EuiFieldPassword, EuiFieldText,
  EuiFormRow, EuiModal, EuiModalBody, EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle,
  EuiOverlayMask, EuiSpacer, EuiText, EuiTitle, EuiToolTip,
  EuiTab, EuiTabs,
} from '@elastic/eui';
import { useQuery } from '@tanstack/react-query';
import { advancedApi } from './index';
import { useConsole } from '../../app-context';
import { copyText } from '../../shared/clipboard';
import { formatDateTime } from '../../shared/format';
import type { ControllerSettings, SensitiveItem } from './types';
import { ControllerIdentityPanel } from './components/ControllerIdentityPanel';

interface RevealState { item: SensitiveItem; purpose: 'reveal' | 'copy' }
interface CopyReadyState { item: SensitiveItem; value: string }

export function AdvancedWorkspace() {
  const { selectedCluster } = useConsole();
  const [grant, setGrant] = useState<{ token: string; expires: number }>();
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState<RevealState>();
  const [copyReady, setCopyReady] = useState<CopyReadyState>();
  const [copyError, setCopyError] = useState('');
  const copyDialogRef = useRef<HTMLDivElement>(null);
  const copyInputRef = useRef<HTMLInputElement>(null);
  const [visible, setVisible] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [tab, setTab] = useState<'sensitive' | 'controller'>('sensitive');
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['sensitive-items', selectedCluster?.id], enabled: Boolean(selectedCluster),
    queryFn: () => advancedApi.sensitiveItems(selectedCluster!.id),
  });
  const { data: controllerSettings } = useQuery({ queryKey: ['controller-settings'], queryFn: advancedApi.controllerSettings });
  useEffect(() => { setGrant(undefined); setVisible({}); setPending(undefined); setCopyReady(undefined); setError(''); setCopyError(''); }, [selectedCluster?.id]);
  useEffect(() => () => setVisible({}), []);
  const groups = useMemo(() => {
    const result = new Map<string, SensitiveItem[]>();
    for (const item of data?.items || []) result.set(item.category, [...(result.get(item.category) || []), item]);
    return result;
  }, [data]);
  const timezone = controllerSettings?.timezone || 'UTC';

  const reveal = async (request: RevealState, token: string) => {
    if (!selectedCluster) return;
    const result = await advancedApi.reveal(selectedCluster.id, request.item.id, token, request.purpose);
    if (request.purpose === 'copy') {
      setCopyError('');
      setCopyReady({ item: request.item, value: result.value });
    } else {
      setVisible((current) => ({ ...current, [request.item.id]: result.value }));
      window.setTimeout(() => setVisible((current) => { const next = { ...current }; delete next[request.item.id]; return next; }), result.hide_after * 1000);
    }
  };
  const copyAuthorizedValue = async () => {
    if (!copyReady) return;
    setCopyError('');
    try {
      await copyText(copyReady.value, copyDialogRef.current || document.body, copyInputRef.current);
      setCopyReady(undefined);
    } catch { setCopyError('Clipboard access was unavailable. The value remains selected for manual copying.'); }
  };
  const requestAccess = async (item: SensitiveItem, purpose: 'reveal' | 'copy') => {
    setError('');
    const request = { item, purpose };
    if (grant && grant.expires > Date.now()) {
      try { await reveal(request, grant.token); } catch (reason) { setGrant(undefined); setPending(request); setError((reason as Error).message); }
    } else setPending(request);
  };
  const reauthenticate = async () => {
    if (!selectedCluster || !pending) return;
    setError('');
    try {
      const response = await advancedApi.revealGrant(selectedCluster.id, password);
      const next = { token: response.grant_token, expires: Date.now() + response.expires_in * 1000 };
      setGrant(next); setPassword(''); await reveal(pending, next.token); setPending(undefined);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Re-authentication failed'); }
  };
  return <div className="page-stack">
    <div className="page-heading"><div><EuiTitle><h1>Advance</h1></EuiTitle><EuiText color="subdued">Controller identity and cluster-scoped credentials, certificates, keys, and enrollment material.</EuiText></div>{tab === 'sensitive' && selectedCluster && <EuiButtonEmpty iconType="refresh" onClick={() => refetch()} isLoading={isLoading}>Refresh inventory</EuiButtonEmpty>}</div>
    <EuiTabs><EuiTab isSelected={tab === 'sensitive'} onClick={() => setTab('sensitive')}>Elastic Stack Secret</EuiTab><EuiTab isSelected={tab === 'controller'} onClick={() => setTab('controller')}>Controller</EuiTab></EuiTabs>
    {tab === 'controller' && <ControllerIdentityPanel />}
    {tab === 'sensitive' && !selectedCluster && <EuiCallOut title="Select or create a cluster" iconType="cluster" />}
    {tab === 'sensitive' && selectedCluster && <>
    <EuiCallOut title="Sensitive access is audited" color="warning" iconType="lock">Values are masked by default. Re-authentication grants access to this cluster for 60 seconds; revealed values hide after 30 seconds.</EuiCallOut>
    {[...groups.entries()].map(([category, items]) => <section className="section-band" key={category}>
      <div className="section-heading"><EuiTitle size="s"><h2>{category}</h2></EuiTitle><EuiBadge>{items.length}</EuiBadge></div>
      <div className="sensitive-list">{items.map((item) => <div className="sensitive-row" key={item.id}>
        <div className="sensitive-row__identity"><strong>{item.label}</strong><small>{item.source}</small>{item.storage_path && <small className="sensitive-row__path">{item.storage_path}</small>}</div>
        <EuiBadge color={item.available ? 'success' : 'warning'}>{item.available ? 'available' : 'unavailable'}</EuiBadge>
        <div className="sensitive-row__metadata">{item.fingerprint && <span title={item.fingerprint}>SHA-256 {item.fingerprint.slice(0, 23)}…</span>}{item.expires_at && <span>Expires {formatDateTime(item.expires_at, timezone)}</span>}</div>
        <EuiFieldText className="secret-value" readOnly value={visible[item.id] || item.masked_value} aria-label={`${item.label} value`} />
        <EuiToolTip content={visible[item.id] ? 'Hide value' : 'Reveal value'}><EuiButtonEmpty iconType={visible[item.id] ? 'eyeClosed' : 'eye'} onClick={() => visible[item.id] ? setVisible((current) => { const next = { ...current }; delete next[item.id]; return next; }) : requestAccess(item, 'reveal')} disabled={!item.available}>{visible[item.id] ? 'Hide' : 'Reveal'}</EuiButtonEmpty></EuiToolTip>
        <EuiToolTip content="Copy with an audited access event"><EuiButtonEmpty iconType="copyClipboard" onClick={() => requestAccess(item, 'copy')} disabled={!item.available}>Copy</EuiButtonEmpty></EuiToolTip>
      </div>)}</div>
    </section>)}
    {!isLoading && !data?.items.length && <EuiCallOut title="No sensitive material is configured" iconType="lock" />}
    {pending && <EuiOverlayMask><EuiModal onClose={() => { setPending(undefined); setError(''); }} initialFocus="[data-autofocus]">
      <EuiModalHeader><EuiModalHeaderTitle>Re-authenticate to {pending.purpose}</EuiModalHeaderTitle></EuiModalHeader>
      <EuiModalBody><EuiText>Enter the current administrator password to access <strong>{pending.item.label}</strong>.</EuiText><EuiSpacer /><EuiFormRow label="Administrator password" isInvalid={Boolean(error)} error={error}><EuiFieldPassword data-autofocus value={password} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') reauthenticate(); }} /></EuiFormRow></EuiModalBody>
      <EuiModalFooter><EuiButtonEmpty onClick={() => setPending(undefined)}>Cancel</EuiButtonEmpty><EuiButton fill onClick={reauthenticate} disabled={!password}>Authorize</EuiButton></EuiModalFooter>
    </EuiModal></EuiOverlayMask>}</>}
    {copyReady && <EuiOverlayMask><EuiModal onClose={() => { setCopyReady(undefined); setCopyError(''); }}><div ref={copyDialogRef}>
      <EuiModalHeader><EuiModalHeaderTitle>Copy {copyReady.item.label}</EuiModalHeaderTitle></EuiModalHeader>
      <EuiModalBody>{copyError && <><EuiCallOut title="Copy failed" color="danger" iconType="warning">{copyError}</EuiCallOut><EuiSpacer /></>}<EuiFormRow label={copyReady.item.label}><EuiFieldText inputRef={copyInputRef} readOnly value={copyReady.value} aria-label={`${copyReady.item.label} copy value`} autoComplete="off" /></EuiFormRow></EuiModalBody>
      <EuiModalFooter><EuiButtonEmpty onClick={() => { setCopyReady(undefined); setCopyError(''); }}>Cancel</EuiButtonEmpty><EuiButton fill iconType="copyClipboard" onClick={copyAuthorizedValue}>Copy to clipboard</EuiButton></EuiModalFooter>
    </div></EuiModal></EuiOverlayMask>}
  </div>;
}
