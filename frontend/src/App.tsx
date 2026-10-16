import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster, toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { ErrorBoundary } from './components/ErrorBoundary';
import { SiteHeader } from './components/layout/SiteHeader';
import { Footer } from './components/layout/Footer';
import { useAuth } from './store/auth';
import { usePermission } from './hooks/usePermission';
import { useTheme } from './store/theme';
import { setLocale } from './i18n';

const LandingPage = lazy(() => import('./pages/LandingPage'));
const AuthPage = lazy(() => import('./pages/auth/AuthPage'));
const SubmitComplaint = lazy(() => import('./pages/SubmitComplaint'));
const TrackComplaint = lazy(() => import('./pages/TrackComplaint'));
const Dashboard = lazy(() => import('./pages/Dashboard'));
const ComplaintDetail = lazy(() => import('./pages/ComplaintDetail'));
const CustomerPortal = lazy(() => import('./pages/CustomerPortal'));
const NotFound = lazy(() => import('./pages/NotFound'));
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 15_000 } } });

function PublicLayout() { return <><SiteHeader/><Outlet/><Footer/></>; }
function Loading() { return <main className="grid min-h-[55vh] place-items-center"><div className="h-10 w-10 animate-spin rounded-full border-2 border-emerald-500 border-t-transparent" aria-label="Loading"/></main>; }
function Forbidden() { const { t } = useTranslation(); return <main className="grid min-h-screen place-items-center bg-cream p-6 text-center dark:bg-[#071627]"><div className="glass-card max-w-lg p-10"><div className="text-6xl font-bold text-gold">403</div><h1 className="mt-4 text-2xl font-bold dark:text-white">{t('errors.forbidden')}</h1><p className="mt-3 text-slate-600 dark:text-slate-300">صلاحياتك قد تتغير في أي وقت. تواصل مع مسؤول النظام إذا كنت تعتقد أن هذا خطأ.</p><a className="mt-6 inline-flex rounded-xl bg-navy px-5 py-3 text-white" href="/">العودة للرئيسية</a></div></main>; }
function CustomerGuard() { const {kind,loading,loadMe}=useAuth(); const location=useLocation(); useEffect(()=>{if(kind==='guest'&&!loading)void loadMe();},[kind,loading,loadMe]); if(loading)return <Loading/>; if(kind==='guest')return <Navigate to="/auth/customer" replace state={{from:location.pathname}}/>; return kind==='customer'?<Outlet/>:<Forbidden/>; }
function StaffGuard() { const { kind, loading, staff, loadMe } = useAuth(); const location = useLocation(); const { hasAny } = usePermission();
  useEffect(() => { if (kind === 'guest' && !loading) void loadMe(); }, [kind, loading, loadMe]);
  if (loading) return <Loading/>;
  if (kind === 'guest') return <Navigate to="/auth/staff" replace state={{ from: location.pathname }}/>;
  if (kind !== 'staff' || !staff) return <Forbidden/>;
  return hasAny(['complaints.view', 'analytics.view', 'users.view']) ? <Outlet/> : <Forbidden/>;
}
function AppRoutes() {
 const loadMe = useAuth((s) => s.loadMe); const kind = useAuth((s) => s.kind);
 const { i18n } = useTranslation(); const theme = useTheme((s) => s.theme); const staff = useAuth((s) => s.staff);
 useEffect(() => { setLocale(i18n.language); }, [i18n.language]);
 useEffect(() => { if (kind === 'guest') void loadMe(); }, [kind, loadMe]);
 useEffect(() => {
   if (!staff) return;
   const channel = new BroadcastChannel('insurance-access');
   channel.onmessage = () => { void loadMe(); queryClient.invalidateQueries(); toast.info('تم تحديث صلاحيات الوصول'); };
   const onStorage = (e: StorageEvent) => { if (e.key === 'insurance-access-version') { void loadMe(); queryClient.invalidateQueries(); } };
   window.addEventListener('storage', onStorage);
   const poll = window.setInterval(() => { void loadMe(); }, 120_000);
   return () => { channel.close(); window.removeEventListener('storage', onStorage); clearInterval(poll); };
 }, [loadMe, staff]);
 return <div className={theme === 'dark' ? 'dark' : ''}><Suspense fallback={<Loading/>}><Routes>
   <Route element={<PublicLayout/>}><Route path="/" element={<LandingPage/>}/><Route path="/auth/:flow" element={<AuthPage/>}/><Route path="/auth" element={<Navigate to="/auth/staff" replace/>}/></Route>
   <Route path="/complaints/new" element={<SubmitComplaint/>}/><Route element={<CustomerGuard/>}><Route path="/portal" element={<CustomerPortal/>}/><Route path="/portal/complaints/:complaintId" element={<CustomerPortal/>}/></Route><Route path="/complaints/track" element={<TrackComplaint/>}/>
   <Route element={<StaffGuard/>}><Route path="/dashboard" element={<Dashboard/>}/><Route path="/dashboard/complaints/:complaintId" element={<ComplaintDetail/>}/></Route>
   <Route path="/403" element={<Forbidden/>}/><Route path="*" element={<NotFound/>}/>
 </Routes></Suspense><Toaster position={i18n.language.startsWith('ar') ? 'top-left' : 'top-right'} richColors closeButton/></div>;
}
export default function App() { return <ErrorBoundary><QueryClientProvider client={queryClient}><BrowserRouter><AppRoutes/></BrowserRouter></QueryClientProvider></ErrorBoundary>; }
