import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { BrandingProvider } from './components/Branding';
import i18n, { applyLanguage, initialLanguage } from './i18n';
// Self-hosted fonts: no third-party requests (CSP `font-src 'self'`, privacy).
import '@fontsource/cairo/400.css';
import '@fontsource/cairo/500.css';
import '@fontsource/cairo/600.css';
import '@fontsource/cairo/700.css';
import '@fontsource/cairo/800.css';
import '@fontsource/tajawal/400.css';
import '@fontsource/tajawal/500.css';
import '@fontsource/tajawal/700.css';
import '@fontsource/tajawal/800.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/inter/800.css';
import '@fontsource/ibm-plex-sans-arabic/400.css';
import '@fontsource/ibm-plex-sans-arabic/500.css';
import '@fontsource/ibm-plex-sans-arabic/600.css';
import '@fontsource/ibm-plex-sans-arabic/700.css';
import './style.css';
import './styles/brokerhub-theme.css';

applyLanguage(initialLanguage());
void i18n.changeLanguage(initialLanguage());
if (localStorage.getItem('insurance-portal:theme') === 'dark' || !localStorage.getItem('insurance-portal:theme')) {
  document.documentElement.classList.add('dark');
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrandingProvider><App /></BrandingProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);

// Registered from the bundle (not an inline script) so CSP can forbid inline JS.
if ('serviceWorker' in navigator && window.location.protocol === 'https:') {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js');
  });
}
