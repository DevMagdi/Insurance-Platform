import { useCallback, useEffect, useState } from 'react';

/** Minimal pathname router (no dependency); deep links work via the dev/nginx SPA fallback. */
export function usePathname(): string {
  const [path, setPath] = useState(() => window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  return path;
}

export function navigate(path: string): void {
  window.history.pushState({}, '', path);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function useNavigate(): (path: string) => void {
  return useCallback((path: string) => navigate(path), []);
}
