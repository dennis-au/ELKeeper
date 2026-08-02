import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  EuiButtonIcon, EuiHeader, EuiHeaderSection, EuiHeaderSectionItem, EuiHeaderLogo,
  EuiSelect, EuiSideNav, EuiSpacer, EuiText,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { queries, setToken } from '../api';
import { ConsoleContext, type NavigationGuard } from '../app-context';
import { ActionConsole } from './ActionConsole';

const pages = [
  { path: '/dashboard', label: 'Dashboard' },
  { path: '/clusters', label: 'Cluster Config' },
  { path: '/hosts', label: 'Host Config' },
  { path: '/roles', label: 'Role Assignment' },
  { path: '/advanced', label: 'Advance' },
];

const mobileNavigationQuery = '(max-width: 760px)';

function isMobileNavigation() {
  return window.matchMedia(mobileNavigationQuery).matches;
}

export function Shell() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: clusters = [] } = useQuery({ queryKey: ['clusters'], queryFn: queries.clusters, refetchInterval: 15000 });
  const [selectedClusterId, setSelectedClusterIdState] = useState<number>(() => Number(localStorage.getItem('elastic-selected-cluster')) || 0);
  const [watchedRunId, setWatchedRunId] = useState<number>();
  const [navOpen, setNavOpen] = useState(() => !isMobileNavigation());
  const [navigationGuard, setNavigationGuard] = useState<NavigationGuard | undefined>();
  const registerNavigationGuard = useCallback((guard?: NavigationGuard) => setNavigationGuard(() => guard), []);
  const runGuarded = useCallback((continueNavigation: () => void) => {
    if (navigationGuard?.(continueNavigation)) return;
    continueNavigation();
  }, [navigationGuard]);

  useEffect(() => {
    const media = window.matchMedia(mobileNavigationQuery);
    const syncNavigationForViewport = () => setNavOpen(!media.matches);
    media.addEventListener('change', syncNavigationForViewport);
    return () => media.removeEventListener('change', syncNavigationForViewport);
  }, []);

  useEffect(() => {
    if (!selectedClusterId && clusters[0]) setSelectedClusterIdState(clusters[0].id);
    if (selectedClusterId && clusters.length && !clusters.some((cluster) => cluster.id === selectedClusterId)) setSelectedClusterIdState(clusters[0].id);
  }, [clusters, selectedClusterId]);

  const setSelectedClusterId = (id: number) => {
    if (id === selectedClusterId) return;
    runGuarded(() => {
      localStorage.setItem('elastic-selected-cluster', String(id));
      setSelectedClusterIdState(id);
    });
  };
  const selectedCluster = clusters.find((cluster) => cluster.id === selectedClusterId);
  const navItems = useMemo(() => [{
    name: 'Management',
    id: 0,
    items: pages.map((page, index) => ({
      id: index + 1,
      name: page.label,
      isSelected: location.pathname === page.path,
      onClick: () => {
        runGuarded(() => {
          navigate(page.path);
          if (isMobileNavigation()) setNavOpen(false);
        });
      },
    })),
  }], [location.pathname, navigate, runGuarded]);
  const refreshAll = () => {
    let refresh = Promise.resolve();
    runGuarded(() => { refresh = queryClient.invalidateQueries(); });
    return refresh;
  };
  const watchRun = (runId?: number) => {
    setWatchedRunId(runId);
    if (runId) void queryClient.invalidateQueries({ queryKey: ['runs'] });
  };

  return (
    <ConsoleContext.Provider value={{ clusters, selectedCluster, selectedClusterId, setSelectedClusterId, watchRun, refreshAll, registerNavigationGuard }}>
      <div className={`app-shell ${navOpen ? 'nav-is-open' : 'nav-is-closed'}`} style={{ '--cluster-accent': selectedCluster?.theme_color || '#0077CC' } as React.CSSProperties}>
        <EuiHeader position="fixed" className="app-header">
          <EuiHeaderSection grow={false}>
            <EuiHeaderSectionItem>
              <EuiButtonIcon
                className="mobile-nav-button"
                iconType="menu"
                aria-controls="primary-navigation"
                aria-expanded={navOpen}
                aria-label={navOpen ? 'Hide navigation' : 'Open navigation'}
                onClick={() => setNavOpen((value) => !value)}
              />
              <EuiHeaderLogo iconType="logoElastic">ELKeeper</EuiHeaderLogo>
            </EuiHeaderSectionItem>
          </EuiHeaderSection>
          <EuiHeaderSection>
            <EuiHeaderSectionItem>
              <label className="cluster-picker">
                <span>Cluster</span>
                <EuiSelect
                  aria-label="Selected cluster"
                  value={selectedClusterId || ''}
                  onChange={(event) => setSelectedClusterId(Number(event.target.value))}
                  options={clusters.length ? clusters.map((cluster) => ({ value: cluster.id, text: cluster.name })) : [{ value: '', text: 'No clusters' }]}
                />
              </label>
            </EuiHeaderSectionItem>
          </EuiHeaderSection>
          <EuiHeaderSection side="right">
            <EuiHeaderSectionItem><EuiButtonIcon iconType="refresh" aria-label="Refresh all data" onClick={refreshAll} /></EuiHeaderSectionItem>
            <EuiHeaderSectionItem><EuiButtonIcon iconType="exit" aria-label="Sign out" onClick={() => runGuarded(() => { setToken(''); navigate('/'); })} /></EuiHeaderSectionItem>
          </EuiHeaderSection>
        </EuiHeader>
        <aside id="primary-navigation" className="app-sidebar">
          <EuiText size="xs" color="subdued"><strong>ELASTIC STACK</strong></EuiText>
          <EuiSpacer size="m" />
          <EuiSideNav aria-label="Primary navigation" items={navItems} mobileBreakpoints={[]} />
        </aside>
        <main className="app-content"><Outlet /></main>
        <ActionConsole collapseKey={location.pathname} watchedRunId={watchedRunId} onWatch={watchRun} />
      </div>
    </ConsoleContext.Provider>
  );
}
