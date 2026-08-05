import { useState } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButtonEmpty, EuiCallOut, EuiConfirmModal, EuiText, EuiTitle,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { advancedApi } from '../api';
import type { HostKeyRecord } from '../types';

export function HostKeyRecordsPanel() {
  const client = useQueryClient();
  const { data, isLoading, error } = useQuery({ queryKey: ['ssh-host-key-records'], queryFn: advancedApi.hostKeyRecords });
  const [pending, setPending] = useState<HostKeyRecord>();
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState('');
  const remove = async () => {
    if (!pending) return;
    setRemoving(true);
    setRemoveError('');
    try {
      await advancedApi.removeHostKeyRecord(pending.node_id);
      await client.invalidateQueries({ queryKey: ['ssh-host-key-records'] });
      setPending(undefined);
    } catch (reason) {
      setRemoveError(reason instanceof Error ? reason.message : 'Unable to remove the SSH host-key record');
    } finally {
      setRemoving(false);
    }
  };
  const columns = [
    { field: 'name', name: 'Host', render: (value: string, item: HostKeyRecord) => <div><strong>{value}</strong><small className="block-muted">{item.address}:{item.ssh_port}</small></div> },
    { field: 'fingerprint', name: 'Recorded fingerprint', render: (value: string) => <code>{value}</code> },
    { name: 'State', render: () => <EuiBadge color="success">pinned</EuiBadge> },
    { field: 'node_id', name: 'Actions', render: (_: number, item: HostKeyRecord) => <EuiButtonEmpty color="danger" size="s" iconType="trash" onClick={() => { setRemoveError(''); setPending(item); }}>Delete record</EuiButtonEmpty> },
  ];
  return <div className="controller-tab">
    <div className="section-heading"><div><EuiTitle size="s"><h2>SSH host-key records</h2></EuiTitle><EuiText color="subdued">Pinned SSH host identities used by controller-to-host connections. Public key material is never displayed here.</EuiText></div></div>
    {error && <EuiCallOut title="Host-key inventory failed" color="danger" iconType="warning">{error instanceof Error ? error.message : 'Unable to load SSH host-key records'}</EuiCallOut>}
    {removeError && <EuiCallOut title="Host-key record removal failed" color="danger" iconType="warning">{removeError}</EuiCallOut>}
    <section className="section-band">
      {isLoading ? <EuiText color="subdued">Loading SSH host-key records...</EuiText> : <EuiBasicTable items={data?.items || []} columns={columns} tableLayout="auto" />}
      {!isLoading && !data?.items.length && <EuiCallOut title="No SSH host-key records" iconType="key" color="primary">Hosts without a recorded pin are not listed.</EuiCallOut>}
    </section>
    {pending && <EuiConfirmModal
      title={`Delete SSH host-key record for ${pending.name}`}
      onCancel={() => { if (!removing) setPending(undefined); }}
      onConfirm={remove}
      cancelButtonText="Cancel"
      confirmButtonText="Delete record"
      buttonColor="danger"
      isLoading={removing}
    >
      <p>This deletes only ELKeeper's recorded pin for <strong>{pending.address}:{pending.ssh_port}</strong>.</p>
      <p>Future controller SSH connections will not strictly verify this host until a new host key is recorded. Verify a replacement fingerprint out of band before adding one.</p>
    </EuiConfirmModal>}
  </div>;
}
