import { lazy, Suspense, type ReactNode } from 'react';
import { motion, useReducedMotion } from 'framer-motion';
import { Layout } from './components/Layout';
import { usePathname } from './lib/router';
import { HomePage } from './pages/HomePage';
import { LoginPage } from './pages/LoginPage';
import { NewComplaintPage } from './pages/NewComplaintPage';
import { InvitePage } from './pages/InvitePage';
import { ProfilePage } from './pages/ProfilePage';
import { StaffAnalyticsPage } from './pages/StaffAnalyticsPage';
import { StaffAuditPage, StaffExportsPage } from './pages/StaffAuditExportsPage';
import { StaffLoginPage } from './pages/StaffLoginPage';
import { TicketDetailPage } from './pages/TicketDetailPage';
import { TicketsPage } from './pages/TicketsPage';
import { TrackPage } from './pages/TrackPage';

const StaffConsole = lazy(() => import('./StaffConsole'));

/** Quiet page entrance: a 220ms fade with a breath of rise, keyed by route. */
function PageTransition({ routeKey, children }: { routeKey: string; children: ReactNode }) {
  const reduced = useReducedMotion();
  if (reduced) return <>{children}</>;
  return (
    <motion.div
      key={routeKey}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.22, 0.61, 0.36, 1] }}
    >
      {children}
    </motion.div>
  );
}

/** Customer portal plus the Phase 4/5 staff workspace and Phase 7 administration. */
function App() {
  const path = usePathname();
  const ticketMatch = path.match(/^\/tickets\/([0-9a-fA-F-]{36})$/);
  const portalTicketMatch = path.match(/^\/portal\/complaints\/([0-9a-fA-F-]{36})$/);
  const staffTicketMatch = path.match(/^\/staff\/tickets\/([0-9a-fA-F-]{36})$/);
  const consoleTicketMatch = path.match(/^\/staff\/complaints\/([0-9a-fA-F-]{36})$/);
  // `/dashboard*` is the legacy staff console URL. It has always been bookmarked
  // and linked from older emails, so route it to the same console and rewrite the
  // address bar once, instead of rendering a leftover page outside the shell.
  const legacyConsole = /^\/dashboard(?:\/|$)/.test(path);
  const staffConsoleRoute = legacyConsole || /^\/staff(?:\/(?:overview|admin|complaints|users|customers|roles|operations|audit|storage))?$/.test(path);

  // A staff ticket belongs to the staff console, not the customer layout:
  // rendering it through <Layout> dropped the console chrome, its dark theme
  // and its ticket workspace whenever the URL was opened directly or reloaded.
  if (consoleTicketMatch || staffTicketMatch) {
    return <Suspense fallback={<div className="min-h-screen p-6 text-center">جارٍ تحميل مساحة الإدارة…</div>}><StaffConsole /></Suspense>;
  }
  if (staffConsoleRoute) {
    if (legacyConsole) window.history.replaceState({}, '', path.replace(/^\/dashboard/, '/staff'));
    return <Suspense fallback={<div className="min-h-screen p-6 text-center">جارٍ تحميل مساحة الإدارة…</div>}><StaffConsole /></Suspense>;
  }

  let page: React.ReactNode;
  if (path === '/staff/login' || path === '/auth/staff' || path === '/auth') page = <StaffLoginPage />;
  else if (path === '/invite') page = <InvitePage />;
  else if (path === '/staff/analytics') page = <StaffAnalyticsPage />;
  else if (path === '/staff/audit') page = <StaffAuditPage />;
  else if (path === '/staff/exports') page = <StaffExportsPage />;
  else if (path === '/') page = <HomePage />;
  else if (path === '/login' || path === '/auth/customer') page = <LoginPage />;
  else if (path === '/track' || path === '/complaints/track') page = <TrackPage />;
  else if (path === '/complaints/new') page = <NewComplaintPage />;
  else if (path === '/tickets' || path === '/portal') page = <TicketsPage />;
  else if (ticketMatch) page = <TicketDetailPage complaintId={ticketMatch[1]} />;
  else if (portalTicketMatch) page = <TicketDetailPage complaintId={portalTicketMatch[1]} />;
  else if (path === '/profile') page = <ProfilePage />;
  else page = <HomePage />;

  return <Layout><PageTransition routeKey={path}>{page}</PageTransition></Layout>;
}

export default App;
