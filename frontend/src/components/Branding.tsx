import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export interface PlatformBrand { platform_name: string; logo_url: string; }
const DEFAULT_BRAND: PlatformBrand = { platform_name: 'Insurance', logo_url: '/insurance-logo.svg' };
const BrandContext = createContext<PlatformBrand>(DEFAULT_BRAND);

export function BrandingProvider({ children }: { children: ReactNode }) {
  const [brand, setBrand] = useState(DEFAULT_BRAND);
  useEffect(() => {
    let active = true;
    const refresh = () => { void fetch('/api/public/config', { headers: { Accept: 'application/json' } })
      .then((response) => response.ok ? response.json() as Promise<Partial<PlatformBrand>> : null)
      .then((value) => {
        if (!active || !value) return;
        const next = {
          platform_name: typeof value.platform_name === 'string' ? value.platform_name : DEFAULT_BRAND.platform_name,
          logo_url: typeof value.logo_url === 'string' ? value.logo_url : DEFAULT_BRAND.logo_url,
        };
        setBrand(next);
        document.title = next.platform_name;
        const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
        if (icon) icon.href = next.logo_url;
      })
      .catch(() => undefined); };
    refresh();
    window.addEventListener('insurance:brand-updated', refresh);
    return () => { active = false; window.removeEventListener('insurance:brand-updated', refresh); };
  }, []);
  return <BrandContext.Provider value={brand}>{children}</BrandContext.Provider>;
}

// Intentional hook export alongside the provider; Vite Fast Refresh preserves hook state.
// eslint-disable-next-line react-refresh/only-export-components
export function usePlatformBrand(): PlatformBrand { return useContext(BrandContext); }
