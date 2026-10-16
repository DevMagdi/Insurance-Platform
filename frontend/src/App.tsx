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

/** Phase 6 customer portal routes (mobile-first, Arabic-first RTL + English). */
function App() {
  const path = usePathname();
  const ticketMatch = path.match(/^\/tickets\/([0-9a-fA-F-]{36})$/);
  const staffTicketMatch = path.match(/^\/staff\/tickets\/([0-9a-fA-F-]{36})$/);

  let page: React.ReactNode;
  if (path === '/staff/login') page = <StaffLoginPage />;
  else if (path === '/staff') page = <StaffDashboardPage />;
  else if (staffTicketMatch) page = <StaffTicketPage complaintId={staffTicketMatch[1]} />;
  else if (path === '/staff/analytics') page = <StaffAnalyticsPage />;
  else if (path === '/staff/admin') page = <StaffAdminPage />;
  else if (path === '/staff/audit') page = <StaffAuditPage />;
  else if (path === '/staff/exports') page = <StaffExportsPage />;
  else if (path === '/') page = <HomePage />;
  else if (path === '/login') page = <LoginPage />;
  else if (path === '/track') page = <TrackPage />;
  else if (path === '/complaints/new') page = <NewComplaintPage />;
  else if (path === '/tickets') page = <TicketsPage />;
  else if (ticketMatch) page = <TicketDetailPage complaintId={ticketMatch[1]} />;
  else if (path === '/profile') page = <ProfilePage />;
  else page = <HomePage />;

  return <Layout>{page}</Layout>;
}

export default App;
