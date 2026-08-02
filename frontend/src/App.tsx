import { useEffect, useState } from 'react';
import { EuiButton, EuiFieldPassword, EuiFieldText, EuiForm, EuiFormRow, EuiPanel, EuiProvider, EuiSpacer, EuiText, EuiTitle } from '@elastic/eui';
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { api, getToken, jsonBody, setToken } from './api';
import { Shell } from './components/Shell';
import { DashboardPage } from './pages/DashboardPage';
import { ClustersPage } from './pages/ClustersPage';
import { HostsPage } from './pages/HostsPage';
import { RolesPage } from './pages/RolesPage';
import { AdvancedPage } from './pages/AdvancedPage';

function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const result = await api<{ token: string }>('/api/auth/login', { method: 'POST', ...jsonBody({ username, password }) });
      setToken(result.token);
      onLogin();
      navigate('/dashboard', { replace: true });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign in failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login-page">
      <EuiPanel className="login-panel" paddingSize="l">
        <EuiText size="s" color="subdued"><strong>ELKEEPER</strong></EuiText>
        <EuiSpacer size="s" />
        <EuiTitle size="l"><h1>Sign in</h1></EuiTitle>
        <EuiSpacer />
        <EuiForm component="form" onSubmit={submit} isInvalid={Boolean(error)} error={error ? [error] : undefined}>
          <EuiFormRow label="Username"><EuiFieldText value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></EuiFormRow>
          <EuiFormRow label="Password"><EuiFieldPassword value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></EuiFormRow>
          <EuiSpacer />
          <EuiButton type="submit" fill isLoading={busy} fullWidth>Sign in</EuiButton>
        </EuiForm>
      </EuiPanel>
    </main>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(Boolean(getToken()));
  useEffect(() => {
    const changed = () => setAuthenticated(Boolean(getToken()));
    const expired = () => { setToken(''); setAuthenticated(false); };
    window.addEventListener('elastic-auth-change', changed);
    window.addEventListener('elastic-auth-expired', expired);
    return () => {
      window.removeEventListener('elastic-auth-change', changed);
      window.removeEventListener('elastic-auth-expired', expired);
    };
  }, []);
  return (
    <EuiProvider colorMode="light">
      <Routes>
        <Route path="/" element={authenticated ? <Navigate to="/dashboard" replace /> : <LoginPage onLogin={() => setAuthenticated(true)} />} />
        {authenticated && <Route element={<Shell />}>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/clusters" element={<ClustersPage />} />
          <Route path="/hosts" element={<HostsPage />} />
          <Route path="/roles" element={<RolesPage />} />
          <Route path="/advanced" element={<AdvancedPage />} />
        </Route>}
        <Route path="*" element={<Navigate to={authenticated ? '/dashboard' : '/'} replace />} />
      </Routes>
    </EuiProvider>
  );
}
