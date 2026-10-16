import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../lib/api';
import { formatDate } from '../lib/status';
import { Field, LoadingState, TextInput, Toggle, primaryButton } from '../components/ui';
import { LoginPage } from './LoginPage';
import { useMe } from '../hooks/useMe';

export function ProfilePage() {
  const { t, i18n } = useTranslation();
  const { me, isLoading: meLoading } = useMe();
  const queryClient = useQueryClient();
  const profileQuery = useQuery({ queryKey: ['profile'], queryFn: api.profile, enabled: Boolean(me) });
  const notificationsQuery = useQuery({
    queryKey: ['my-notifications'],
    queryFn: api.notifications,
    enabled: Boolean(me),
  });

  const profile = profileQuery.data;
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [whatsapp, setWhatsapp] = useState(false);
  const [notifyEmail, setNotifyEmail] = useState(true);
  const [notifyWhatsapp, setNotifyWhatsapp] = useState(true);
  const [notifySms, setNotifySms] = useState(true);
  const [saved, setSaved] = useState(false);

  // Sync the form when the profile first loads (adjust-during-render pattern).
  const [syncedFor, setSyncedFor] = useState<string | null>(null);
  if (profile && syncedFor !== profile.id) {
    setSyncedFor(profile.id);
    setFullName(profile.full_name);
    setEmail(profile.email ?? '');
    setWhatsapp(profile.whatsapp_available);
    setNotifyEmail(profile.notification_preferences.email);
    setNotifyWhatsapp(profile.notification_preferences.whatsapp);
    setNotifySms(profile.notification_preferences.sms);
  }

  const save = useMutation({
    mutationFn: () =>
      api.updateProfile({
        full_name: fullName.trim(),
        email: email.trim() ? email.trim().toLowerCase() : null,
        whatsapp_available: whatsapp,
        notification_preferences: { email: notifyEmail, whatsapp: notifyWhatsapp, sms: notifySms },
      }),
    onSuccess: () => {
      setSaved(true);
      void queryClient.invalidateQueries({ queryKey: ['profile'] });
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });

  const markRead = useMutation({
    mutationFn: (id: string) => api.markNotificationRead(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['my-notifications'] }),
  });

  if (meLoading) return <LoadingState />;
  if (!me) return <LoginPage />;

  return (
    <section className="mx-auto max-w-2xl space-y-5">
      <div>
        <h1 className="text-2xl font-bold">{t('profile.title')}</h1>
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{t('profile.subtitle')}</p>
      </div>

      <form
        className="space-y-4 rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5"
        onSubmit={(e) => {
          e.preventDefault();
          setSaved(false);
          save.mutate();
        }}
      >
        <Field label={t('wizard.fullName')}>
          <TextInput value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" />
        </Field>
        <Field label={t('wizard.email')}>
          <TextInput value={email} onChange={(e) => setEmail(e.target.value)} inputMode="email" dir="ltr" autoComplete="email" />
        </Field>
        <div className="grid gap-3 text-sm sm:grid-cols-2">
          <p className="rounded-xl bg-slate-900/[0.03] px-3 py-2 dark:bg-white/5">
            <span className="block text-slate-500">{t('profile.mobile')}</span>
            <span className="font-medium" dir="ltr">{profile?.mobile ?? me.mobile}</span>
          </p>
          <p className="rounded-xl bg-slate-900/[0.03] px-3 py-2 dark:bg-white/5">
            <span className="block text-slate-500">{t('profile.nationalId')}</span>
            <span className="font-medium" dir="ltr">{profile?.national_id_masked ?? me.national_id_masked}</span>
          </p>
        </div>
        <Toggle checked={whatsapp} onChange={setWhatsapp} label={t('wizard.whatsapp')} />

        <fieldset className="space-y-2.5 rounded-2xl border border-slate-200/80 p-4 dark:border-white/10">
          <legend className="px-1 font-semibold">{t('profile.prefsTitle')}</legend>
          <Toggle checked={notifyEmail} onChange={setNotifyEmail} label={t('profile.email')} />
          <Toggle checked={notifyWhatsapp} onChange={setNotifyWhatsapp} label={t('profile.whatsappChannel')} />
          <Toggle checked={notifySms} onChange={setNotifySms} label={t('profile.sms')} />
        </fieldset>

        {saved && <p className="text-sm text-emerald-500" role="status">{t('profile.saved')}</p>}
        {save.isError && <p className="text-sm text-rose-500" role="alert">{t('errors.generic')}</p>}
        <button type="submit" className={primaryButton} disabled={save.isPending || fullName.trim().length < 2}>
          {t('common.save')}
        </button>
      </form>

      <section className="rounded-3xl border border-slate-200/80 bg-white/70 p-5 backdrop-blur dark:border-white/10 dark:bg-white/5">
        <h2 className="mb-3 font-semibold">{t('profile.notifications')}</h2>
        {(notificationsQuery.data ?? []).length === 0 && (
          <p className="text-sm text-slate-500">{t('profile.noNotifications')}</p>
        )}
        <ul className="space-y-2.5">
          {notificationsQuery.data?.map((note) => (
            <li key={note.id} className={`flex items-center justify-between gap-3 rounded-xl px-3 py-2 text-sm ${note.read_at ? 'bg-slate-900/[0.02] dark:bg-white/[0.03]' : 'bg-amber-400/10'}`}>
              <span>
                <span className="font-medium" dir="ltr">
                  {typeof note.payload.ticket_no === 'string' ? note.payload.ticket_no : note.kind}
                </span>
                <span className="block text-xs text-slate-500">{formatDate(note.created_at, i18n.language)}</span>
              </span>
              {!note.read_at && (
                <button type="button" className="shrink-0 text-xs font-semibold text-amber-600 dark:text-amber-300" onClick={() => markRead.mutate(note.id)}>
                  {t('profile.markRead')}
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>
    </section>
  );
}
