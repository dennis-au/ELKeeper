import { useEffect, useState } from 'react';
import { EuiButton, EuiFieldPassword, EuiFieldText, EuiForm, EuiFormRow, EuiPanel, EuiProvider, EuiSpacer, EuiText, EuiTitle } from '@elastic/eui';
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { authApi } from './features/auth';
import { Shell } from './components/Shell';
import { DashboardPage } from './pages/DashboardPage';
import { ClustersPage } from './pages/ClustersPage';
import { HostsPage } from './pages/HostsPage';
import { RolesPage } from './pages/RolesPage';
import { AdvancedPage } from './pages/AdvancedPage';

function ElkeeperLogo({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`elkeeper-logo${compact ? ' is-compact' : ''}`}>
      <svg
        className="elkeeper-logo__mark"
        viewBox="0 0 48 48"
        role={compact ? undefined : 'img'}
        aria-label={compact ? undefined : 'ELKeeper logo'}
        aria-hidden={compact || undefined}
      >
        <rect x="1" y="1" width="46" height="46" rx="7" fill="currentColor" />
        <path d="M14 14h20M14 24h15M14 34h20" fill="none" stroke="white" strokeLinecap="round" strokeWidth="4" />
        <circle cx="35" cy="24" r="4" fill="#00b894" />
        <circle cx="14" cy="14" r="2.5" fill="#f5a623" />
        <circle cx="14" cy="34" r="2.5" fill="#4aa3ff" />
      </svg>
      <div className="elkeeper-logo__wordmark">
        <strong>ELKEEPER</strong>
        {!compact && <span>Elastic Stack control plane</span>}
      </div>
    </div>
  );
}

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
      const result = await authApi.login(username, password);
      authApi.setToken(result.token);
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
      <section className="login-brand" aria-label="ELKeeper">
        <ElkeeperLogo />
        <div className="login-brand__signal" aria-hidden="true">
          <div><span /><span /><span /></div>
          <div><span /><span /><span /><span /></div>
          <div><span /><span /></div>
        </div>
        <div className="login-brand__footer">
          <span className="login-brand__status" />
          Controller access
        </div>
      </section>

      <section className="login-auth">
        <div className="login-auth__content">
          <div className="login-auth__mobile-logo"><ElkeeperLogo compact /></div>
          <EuiText size="s" color="subdued"><strong>OPERATOR CONSOLE</strong></EuiText>
          <EuiSpacer size="s" />
          <EuiTitle size="l"><h1>Sign in</h1></EuiTitle>
          <EuiSpacer size="xs" />
          <EuiText color="subdued"><p>Use your administrator account to continue.</p></EuiText>
          <EuiSpacer size="xl" />
          <EuiPanel className="login-panel" paddingSize="none" hasShadow={false} hasBorder={false}>
            <EuiForm component="form" onSubmit={submit} isInvalid={Boolean(error)} error={error ? [error] : undefined}>
              <EuiFormRow label="Username" fullWidth>
                <EuiFieldText
                  value={username}
                  onChange={(event) => { setUsername(event.target.value); setError(''); }}
                  autoComplete="username"
                  isInvalid={Boolean(error)}
                  fullWidth
                />
              </EuiFormRow>
              <EuiFormRow label="Password" fullWidth>
                <EuiFieldPassword
                  type="dual"
                  value={password}
                  onChange={(event) => { setPassword(event.target.value); setError(''); }}
                  autoComplete="current-password"
                  isInvalid={Boolean(error)}
                  autoFocus
                  fullWidth
                />
              </EuiFormRow>
              <EuiSpacer size="l" />
              <EuiButton type="submit" fill isLoading={busy} disabled={busy || !username.trim() || !password} fullWidth>Sign in</EuiButton>
            </EuiForm>
          </EuiPanel>
          <p className="login-auth__notice">Authorized operators only</p>
        </div>
      </section>
    </main>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(Boolean(authApi.token()));
  useEffect(() => {
    const changed = () => setAuthenticated(Boolean(authApi.token()));
    const expired = () => { authApi.setToken(''); setAuthenticated(false); };
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
