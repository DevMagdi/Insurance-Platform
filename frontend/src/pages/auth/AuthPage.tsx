import { useState, type FormEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { KeyRound, ShieldCheck, UserRound } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { apiFetch } from '../../lib/api/client';
import { useAuth } from '../../store/auth';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';
import { Card } from '../../components/ui/Showcase';

export default function AuthPage() {
 const { flow = 'staff' } = useParams(); const [params] = useSearchParams(); const [step,setStep] = useState<'credentials'|'challenge'|'otp'|'password-change'>('credentials');
 const [challenge,setChallenge] = useState(''); const [mobile,setMobile] = useState(''); const [busy,setBusy] = useState(false); const navigate=useNavigate(); const loadMe=useAuth((s)=>s.loadMe); const {t}=useTranslation();
 const request = async (url:string, body:unknown) => { const r=await apiFetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const data=await r.json().catch(()=>({})); if(!r.ok) throw new Error(data.detail || 'تعذر إكمال الطلب'); return data as Record<string,unknown>; };
 const run = async (e:FormEvent<HTMLFormElement>) => { e.preventDefault(); setBusy(true); const fd=new FormData(e.currentTarget);
  try {
   if(flow==='customer') {
    if(step==='credentials') { const m=String(fd.get('mobile')); await request('/api/auth/customer/otp/start',{mobile:m}); setMobile(m); setStep('otp'); toast.success('إذا كان الرقم مؤهلاً، تم إرسال رمز تحقق.'); }
    else { await request('/api/auth/customer/otp/verify',{mobile,code:fd.get('code'),national_id:fd.get('national_id')||undefined,full_name:fd.get('full_name')||undefined,whatsapp_available:false}); await loadMe(); navigate('/complaints/new'); }
   } else if(flow==='accept-invite') { await request('/api/auth/staff/invitation/accept',{token:params.get('token')||fd.get('token'),password:fd.get('password')}); toast.success('تم قبول الدعوة. سجّل الدخول للمتابعة.'); navigate('/auth/staff'); }
   else if(step==='credentials') { const data=await request('/api/auth/staff/login',{email:fd.get('email'),password:fd.get('password')}); if(data.next_step==='authenticated') { await loadMe(); navigate('/dashboard'); } else { setChallenge(String(data.challenge||'')); setStep('challenge'); if(data.next_step==='change_password') toast.info('يلزم تغيير كلمة المرور قبل المتابعة'); } }
   else { await request('/api/auth/staff/2fa/verify',{challenge,code:fd.get('code')}); await loadMe(); navigate('/dashboard'); }
  } catch(err) { toast.error(err instanceof Error?err.message:'حدث خطأ'); } finally {setBusy(false);} };
 const customer=flow==='customer'; const accept=flow==='accept-invite'; const title=customer?t('auth.customerOtpTitle'):accept?t('auth.acceptInviteTitle'):t('auth.staffLoginTitle');
 return <main className="grid min-h-[75vh] place-items-center bg-cream px-5 py-16 dark:bg-[#071627]"><Card className="w-full max-w-lg p-7 md:p-9"><Link to="/" className="mb-8 flex items-center gap-3"><span className="grid size-11 place-items-center rounded-xl bg-gold text-navy"><ShieldCheck/></span><span className="font-bold">درع<span className="block text-[10px] tracking-widest text-emerald-600">INSURANCE CARE</span></span></Link><p className="text-xs font-bold tracking-widest text-emerald-600">SECURE ACCESS</p><h1 className="mt-2 text-2xl font-bold">{title}</h1><p className="mt-2 text-sm text-slate-500">{customer?'أدخل رقم الهاتف المسجل لاستلام رمز لمرة واحدة.':accept?'أنشئ كلمة مرور آمنة لتفعيل حساب الموظف.':'حسابات الموظفين محمية وقد تتطلب تحققاً بخطوتين.'}</p>
 <form onSubmit={run} className="mt-7 space-y-4">
 {customer&&step==='credentials'&&<label className="block text-sm">{t('auth.mobile')}<Input autoComplete="tel" type="tel" required name="mobile" dir="ltr" placeholder="+20 1XXXXXXXXX"/></label>}
 {customer&&step==='otp'&&<><div className="rounded-xl bg-emerald-50 p-3 text-sm text-emerald-900 dark:bg-emerald-400/10 dark:text-emerald-100">أرسلنا الرمز إلى {mobile}</div><label className="block text-sm">{t('auth.otpCode')}<Input name="code" inputMode="numeric" pattern="[0-9]{6}" minLength={6} maxLength={6} required autoComplete="one-time-code"/></label><label className="block text-sm">رقم الهوية الوطنية (عند الحاجة)<Input name="national_id" inputMode="numeric" pattern="[0-9]{14}" maxLength={14}/></label><label className="block text-sm">الاسم الكامل (للتسجيل الجديد)<Input name="full_name" minLength={2}/></label></>}
 {flow==='staff'&&step==='credentials'&&<><label className="block text-sm">{t('auth.email')}<Input required type="email" name="email" autoComplete="username"/></label><label className="block text-sm">{t('auth.password')}<Input required type="password" name="password" autoComplete="current-password"/></label></>}
 {flow==='staff'&&step==='password-change'&&<label className="block text-sm">كلمة المرور الجديدة<Input required type="password" name="new_password" minLength={12} maxLength={128} autoComplete="new-password"/><small className="text-slate-500">12 حرفاً على الأقل</small></label>}
 {flow==='staff'&&step==='challenge'&&<><div className="rounded-xl bg-gold/10 p-4 text-sm">{t('auth.twoFactor')}</div><label className="block text-sm">رمز التحقق المكون من 6 أرقام<Input required name="code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6}/></label></>}
 {accept&&<><input type="hidden" name="token" value={params.get('token')||''}/><label className="block text-sm">كلمة المرور الجديدة<Input required type="password" minLength={12} maxLength={128} name="password" autoComplete="new-password"/><small className="text-slate-500">12 حرفاً على الأقل</small></label></>}
 <Button className="w-full" disabled={busy} type="submit">{busy?'جارٍ التحقق…':customer?step==='otp'?t('auth.verify'):t('auth.sendOtp'):accept?t('auth.accept'):step==='challenge'?'تحقق بخطوتين':step==='password-change'?'تحديث كلمة المرور':t('auth.signIn')}</Button>
 </form>
 <div className="mt-6 flex flex-wrap justify-between gap-3 border-t pt-5 text-sm dark:border-white/10"><Link className="text-emerald-700 hover:underline" to={customer?'/auth/staff':'/auth/customer'}>{customer?'دخول الموظفين':'دخول العملاء'}</Link>{!customer&&!accept&&<span className="flex items-center gap-1 text-xs text-slate-500"><KeyRound size={14}/> اتصال محمي</span>}{customer&&<span className="flex items-center gap-1 text-xs text-slate-500"><UserRound size={14}/> لا تشارك رمزك مع أحد</span>}</div>
 </Card></main>;
}
