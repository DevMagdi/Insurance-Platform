import { lazy, Suspense } from 'react';
import { Layout } from './components/Layout';
import { usePathname } from './lib/router';
import { HomePage } from './pages/HomePage';
import { LoginPage } from './pages/LoginPage';
import { NewComplaintPage } from './pages/NewComplaintPage';
import { ProfilePage } from './pages/ProfilePage';
import { StaffAdminPage } from './pages/StaffAdminPage';
import { StaffAnalyticsPage } from './pages/StaffAnalyticsPage';
import { StaffAuditPage, StaffExportsPage } from './pages/StaffAuditExportsPage';
import { StaffDashboardPage } from './pages/StaffDashboardPage';
import { StaffLoginPage } from './pages/StaffLoginPage';
import { StaffTicketPage } from './pages/StaffTicketPage';
import { TicketDetailPage } from './pages/TicketDetailPage';
import { TicketsPage } from './pages/TicketsPage';
import { TrackPage } from './pages/TrackPage';

const StaffConsole = lazy(() => import('./StaffConsole'));

/** Customer portal plus the Phase 4/5 staff workspace and Phase 7 administration. */
function App() {
  const path = usePathname();
  const ticketMatch = path.match(/^\/tickets\/([0-9a-fA-F-]{36})$/);
  const portalTicketMatch = path.match(/^\/portal\/complaints\/([0-9a-fA-F-]{36})$/);
  const staffTicketMatch = path.match(/^\/staff\/tickets\/([0-9a-fA-F-]{36})$/);
  const consoleTicketMatch = path.match(/^\/staff\/complaints\/([0-9a-fA-F-]{36})$/);
  const dashboardTicketMatch = path.match(/^\/dashboard\/complaints\/([0-9a-fA-F-]{36})$/);
  const staffConsoleRoute = /^\/staff\/(?:overview|complaints|users|roles|operations|audit)$/.test(path);

  if (consoleTicketMatch) {
    return <Layout><StaffTicketPage complaintId={consoleTicketMatch[1]} /></Layout>;
  }
  if (staffConsoleRoute) return <Suspense fallback={<div className="min-h-screen p-6 text-center">جارٍ تحميل مساحة الإدارة…</div>}><StaffConsole /></Suspense>;

  let page: React.ReactNode;
  if (path === '/staff/login' || path === '/auth/staff' || path === '/auth') page = <StaffLoginPage />;
  else if (path === '/staff' || path === '/dashboard') page = <StaffDashboardPage />;
  else if (staffTicketMatch) page = <StaffTicketPage complaintId={staffTicketMatch[1]} />;
  else if (dashboardTicketMatch) page = <StaffTicketPage complaintId={dashboardTicketMatch[1]} />;
  else if (path === '/staff/analytics') page = <StaffAnalyticsPage />;
  else if (path === '/staff/admin') page = <StaffAdminPage />;
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

  return <Layout>{page}</Layout>;
}

export default App;
