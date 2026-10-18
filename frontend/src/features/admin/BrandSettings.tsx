import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, jsonBody } from '../../api';
import { Icon } from '../../components/Icon';
import type { StaffAccess } from '../../types';
import { hasPermission } from '../../utils/access';

interface Brand { platform_name: string; logo_url: string; }
export function BrandSettings({ access }: { access: StaffAccess }) {
  const [brand, setBrand] = useState<Brand>({ platform_name: 'Insurance', logo_url: '/insurance-logo.svg' });
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const brandTouched = useRef(false);
  const allowedScope = access.all_companies && access.company_ids.length === 0 && access.insurance_types.length === 0 && access.ownership === 'all';
  const allowed = allowedScope && hasPermission(access, 'settings.manage');
  useEffect(() => { fetch('/api/public/config').then((r) => r.ok ? r.json() as Promise<Brand> : null).then((value) => { if (!brandTouched.current && value?.platform_name && value.logo_url) setBrand(value); }).catch(() => undefined); }, []);
  if (!allowed) return null;
  const uploadLogo = async (file: File | undefined) => {
    if (!file) return;
    brandTouched.current = true;
    if (file.size > 2 * 1024 * 1024) { setError('حجم الشعار يجب ألا يتجاوز 2 ميجابايت.'); return; }
    setUploading(true); setError(''); setMessage('');
    try {
      const saved = await api<{ logo_url: string }>('/api/platform/brand/logo', {
        method: 'POST', body: file, headers: { 'Content-Type': file.type },
      });
      setBrand((current) => ({ ...current, logo_url: saved.logo_url }));
      window.dispatchEvent(new Event('insurance:brand-updated'));
      setMessage('تم رفع الشعار وتحديث هوية المنصة.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'تعذر رفع الشعار.'); }
    finally { setUploading(false); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError(''); setMessage('');
    try {
      const saved = await api<Brand>('/api/platform/brand', { method: 'PUT', body: jsonBody(brand) });
      setBrand(saved);
      window.dispatchEvent(new Event('insurance:brand-updated'));
      setMessage('تم تحديث هوية المنصة بنجاح.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'تعذر حفظ الهوية.'); }
    finally { setSaving(false); }
  };
  return <section className="surface-card" style={{ padding: 20, marginBottom: 20 }}>
    <h2 style={{ marginTop: 0 }}>هوية المنصة</h2><p className="muted">تغيير اسم المنصة وشعارها. يقتصر الحفظ فعلياً على Platform Super Admin.</p>
    {message && <p className="inline-alert success" role="status">{message}</p>}{error && <p className="inline-alert error" role="alert">{error}</p>}
    <form onSubmit={(event) => { void submit(event); }} style={{ display: 'grid', gap: 12, maxWidth: 620 }}>
      <label className="field-label">اسم المنصة<input required minLength={2} maxLength={80} value={brand.platform_name} onChange={(event) => { brandTouched.current = true; setBrand({ ...brand, platform_name: event.target.value }); }} /></label>
      <label className="field-label">رابط/مسار الشعار<input required maxLength={2048} value={brand.logo_url} dir="ltr" onChange={(event) => { brandTouched.current = true; setBrand({ ...brand, logo_url: event.target.value }); }} placeholder="/insurance-logo.svg" /></label>
      <label className="field-label">أو ارفع صورة شعار (PNG أو JPEG أو WebP، بحد أقصى 2 ميجابايت)<input type="file" accept="image/png,image/jpeg,image/webp" disabled={uploading} onChange={(event) => { void uploadLogo(event.currentTarget.files?.[0]); event.currentTarget.value = ''; }} /></label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}><img src={brand.logo_url} alt="معاينة الشعار" style={{ width: 44, height: 44, objectFit: 'contain' }} /><button className="button button-dark" type="submit" disabled={saving || uploading}><Icon name="check" size={15} />{uploading ? 'جارٍ رفع الشعار…' : saving ? 'جارٍ الحفظ…' : 'حفظ الهوية'}</button></div>
    </form>
  </section>;
}
