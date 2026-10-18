import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { api, jsonBody } from '../api';
import { Field, TextInput, primaryButton } from '../components/ui';
import { captchaHeaders, useCaptcha } from '../lib/captcha';

interface InvitationDetails {
  email: string;
  full_name: string;
  department_name: string;
  kind: 'invitation' | 'password_reset';
}

export function InvitePage() {
  const { i18n } = useTranslation();
  const ar = i18n.language.startsWith('ar');
  const [token] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('token') ?? new URLSearchParams(window.location.search).get('token') ?? '');
  const [details, setDetails] = useState<InvitationDetails | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [name, setName] = useState('');
  const [nationalId, setNationalId] = useState('');
  const [mobile, setMobile] = useState('');
  const [address, setAddress] = useState('');
  const [city, setCity] = useState('');
  const [jobTitle, setJobTitle] = useState('');
  const [emergencyPhone, setEmergencyPhone] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [photo, setPhoto] = useState('');
  const [photoError, setPhotoError] = useState('');
  const [readingPhoto, setReadingPhoto] = useState(false);
  const photoRead = useRef(0);
  const captcha = useCaptcha('invite_accept');

  useEffect(() => {
    // Bearer tokens must not remain in history or be exposed to third-party widgets.
    window.history.replaceState(window.history.state, '', window.location.pathname);
    if (!token) return;
    const controller = new AbortController();
    void api<InvitationDetails>('/api/auth/staff/invitation/inspect', { method: 'POST', body: jsonBody({ token }), signal: controller.signal }).then((invitation) => {
      if (!controller.signal.aborted) { setDetails(invitation); setName(invitation.full_name); }
    }).catch(() => {
      if (!controller.signal.aborted) setError(ar ? 'الدعوة غير صالحة أو انتهت صلاحيتها. اطلب من الأدمن إعادة إرسالها.' : 'This link is invalid or expired. Ask your administrator to resend it.');
    });
    return () => controller.abort();
  }, [token, ar]);

  const readPhoto = (file?: File) => {
    const reading = ++photoRead.current;
    setReadingPhoto(false);
    setPhoto('');
    setPhotoError('');
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 512 * 1024) {
      setPhotoError(ar ? 'اختر صورة JPEG أو PNG أو WebP لا تتجاوز 512 كيلوبايت.' : 'Choose a JPEG, PNG or WebP photo up to 512 KiB.');
      return;
    }
    setReadingPhoto(true);
    const reader = new FileReader();
    reader.onload = () => { if (reading === photoRead.current) { setPhoto(String(reader.result)); setReadingPhoto(false); } };
    reader.onerror = () => { if (reading === photoRead.current) { setPhotoError(ar ? 'تعذر قراءة الصورة.' : 'Could not read the photo.'); setReadingPhoto(false); } };
    reader.readAsDataURL(file);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError('');
    if (!details || !captcha.ready || readingPhoto) return;
    if (password !== confirmation) { setError(ar ? 'كلمتا المرور غير متطابقتين.' : 'Passwords do not match.'); return; }
    if (details.kind === 'invitation' && !photo) { setPhotoError(ar ? 'الصورة الشخصية مطلوبة.' : 'A profile photo is required.'); return; }
    setBusy(true);
    try {
      await api('/api/auth/staff/invitation/accept', {
        method: 'POST', headers: captchaHeaders(captcha.token),
        body: jsonBody({ token, password, ...(details.kind === 'invitation' ? {
          profile: { full_name: name.trim(), national_id: nationalId, mobile, address: address.trim(), city: city.trim(), job_title: jobTitle.trim(), emergency_phone: emergencyPhone || null },
          avatar_base64: photo.slice(photo.indexOf(',') + 1),
        } : {}) }),
      });
      setDone(true);
      setPassword(''); setConfirmation(''); setPhoto(''); setNationalId('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : (ar ? 'تعذر إنشاء الحساب.' : 'Could not create your account.'));
    } finally { setBusy(false); captcha.reset(); }
  };

  if (done) return <section className="mx-auto max-w-xl rounded-3xl bg-white p-6 shadow-xl dark:bg-slate-900" aria-live="polite"><h1 className="text-2xl font-black">{ar ? 'تم حفظ حسابك بنجاح' : 'Your account is ready'}</h1><p className="my-4">{ar ? 'سجّل الدخول بكلمة المرور الجديدة. صلاحيات العمل يحددها الأدمن.' : 'Sign in with your new password. Your administrator assigns work permissions.'}</p><a className={primaryButton} href="/staff/login">{ar ? 'تسجيل الدخول' : 'Sign in'}</a></section>;

  return <section className="mx-auto max-w-2xl rounded-3xl border border-white/40 bg-white/80 p-6 shadow-xl dark:bg-slate-900">
    <h1 className="text-2xl font-black">{details?.kind === 'password_reset' ? (ar ? 'إعادة تعيين كلمة المرور' : 'Reset password') : (ar ? 'إنشاء الحساب والبروفايل' : 'Create your account and profile')}</h1>
    {error && <p role="alert" className="my-4 text-red-600">{error}</p>}
    {!token && <p role="alert" className="mt-4">{ar ? 'رابط الدعوة غير مكتمل. اطلب رابطًا جديدًا من الأدمن.' : 'The invitation link is incomplete. Ask your administrator for a new link.'}</p>}
    {token && !details && !error && <p role="status" className="mt-4">{ar ? 'جارٍ التحقق من الدعوة…' : 'Checking your invitation…'}</p>}
    {details && <form className="mt-5 space-y-4" onSubmit={(event) => { void submit(event); }}>
      <p className="rounded-xl bg-slate-500/10 p-3"><span dir="ltr">{details.email}</span> · {details.department_name}</p>
      {details.kind === 'invitation' && <>
        <Field label={ar ? 'الاسم الكامل' : 'Full name'}><TextInput required autoComplete="name" minLength={2} maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></Field>
        <Field label={ar ? 'الرقم القومي' : 'National ID'}><TextInput required dir="ltr" inputMode="numeric" pattern="[23][0-9]{13}" maxLength={14} value={nationalId} onChange={(event) => setNationalId(event.target.value)} /></Field>
        <Field label={ar ? 'رقم الهاتف' : 'Phone number'}><TextInput required type="tel" dir="ltr" autoComplete="tel" pattern="\+201[0-9]{9}" placeholder="+201xxxxxxxxx" value={mobile} onChange={(event) => setMobile(event.target.value)} /></Field>
        <Field label={ar ? 'العنوان' : 'Address'}><TextInput required autoComplete="street-address" minLength={5} maxLength={500} value={address} onChange={(event) => setAddress(event.target.value)} /></Field>
        <Field label={ar ? 'المدينة / المحافظة' : 'City / Governorate'}><TextInput required autoComplete="address-level2" minLength={2} maxLength={120} value={city} onChange={(event) => setCity(event.target.value)} /></Field>
        <Field label={ar ? 'المسمى الوظيفي (اختياري)' : 'Job title (optional)'}><TextInput autoComplete="organization-title" maxLength={120} value={jobTitle} onChange={(event) => setJobTitle(event.target.value)} /></Field>
        <Field label={ar ? 'هاتف الطوارئ (اختياري)' : 'Emergency phone (optional)'}><TextInput type="tel" dir="ltr" pattern="\+201[0-9]{9}" placeholder="+201xxxxxxxxx" value={emergencyPhone} onChange={(event) => setEmergencyPhone(event.target.value)} /></Field>
        <Field label={ar ? 'الصورة الشخصية' : 'Profile photo'}><input className="w-full" type="file" accept="image/jpeg,image/png,image/webp" required onChange={(event) => readPhoto(event.target.files?.[0])} /><small>{ar ? 'JPEG / PNG / WebP — بحد أقصى 512 كيلوبايت. يتم حذف بيانات الموقع من الصورة.' : 'JPEG / PNG / WebP — up to 512 KiB. Photo location metadata is removed.'}</small></Field>
        {photoError && <p role="alert" className="text-red-600">{photoError}</p>}
        {photo && <img className="h-24 w-24 rounded-full object-cover" src={photo} alt={ar ? 'معاينة الصورة الشخصية' : 'Profile photo preview'} />}
      </>}
      <Field label={ar ? 'كلمة المرور' : 'Password'}><TextInput required type="password" autoComplete="new-password" minLength={12} maxLength={128} value={password} onChange={(event) => setPassword(event.target.value)} /><small>{ar ? '12 حرفًا على الأقل، تشمل حروفًا وأرقامًا، وليست كلمة شائعة.' : 'At least 12 characters, including letters and digits; not a common password.'}</small></Field>
      <Field label={ar ? 'تأكيد كلمة المرور' : 'Confirm password'}><TextInput required type="password" autoComplete="new-password" minLength={12} maxLength={128} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></Field>
      {captcha.widget}
      {captcha.failed && <p role="alert">{ar ? 'تعذر التحقق الأمني. أعد تحميل الصفحة وحاول مجددًا.' : 'Security verification failed. Reload and try again.'}</p>}
      <button className={`${primaryButton} w-full`} type="submit" disabled={busy || !captcha.ready || readingPhoto}>{busy ? (ar ? 'جارٍ الحفظ…' : 'Saving…') : (ar ? 'حفظ الحساب' : 'Save account')}</button>
    </form>}
  </section>;
}
