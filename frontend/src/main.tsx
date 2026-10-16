import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import i18n, { applyLanguage, initialLanguage } from './i18n';
import './style.css';

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
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
