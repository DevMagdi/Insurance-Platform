import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Bell, BellRing, CheckCheck, Mail, MessageCircle, ShieldCheck, Smartphone, UserRound } from 'lucide-react';
import { api } from '../lib/api';
import { formatDate } from '../lib/status';
import { Field, LoadingState, TextInput, Toggle, Button } from '../components/ui';
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

  const notifications = notificationsQuery.data ?? [];
  const unread = notifications.filter((note) => !note.read_at).length;

  return (
    <div>
      <div className="page-intro">
        <div>
          <span className="eyebrow">{t('profileExtra.eyebrow')}</span>
          <h1>{t('profile.title')}</h1>
          <p>{t('profile.subtitle')}</p>
        </div>
      </div>

      <div className="profile-grid">
        <section className="card profile-card">
          <h2>{t('profileExtra.personal')}</h2>
          <p>{t('profileExtra.personalDesc')}</p>

          <form
            className="profile-form"
            onSubmit={(e) => {
              e.preventDefault();
              setSaved(false);
              save.mutate();
            }}
          >
            <Field label={t('wizard.fullName')}>
              <TextInput value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" />
            </Field>
            <Field label={t('wizard.email')} hint={t('wizardExtra.emailHint')}>
              <TextInput value={email} onChange={(e) => setEmail(e.target.value)} inputMode="email" dir="ltr" autoComplete="email" />
            </Field>

            <div className="readonly-pair">
              <div className="readonly-field">
                <span>{t('profile.mobile')}</span>
                <strong dir="ltr">{profile?.mobile ?? me.mobile}</strong>
              </div>
              <div className="readonly-field">
                <span>{t('profile.nationalId')}</span>
                <strong dir="ltr">{profile?.national_id_masked ?? me.national_id_masked}</strong>
              </div>
            </div>

            <Toggle checked={whatsapp} onChange={setWhatsapp} label={t('wizard.whatsapp')} />

            <fieldset className="pref-group" style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="pref-group-title">{t('profileExtra.preferences')}</legend>
              <p className="field-hint" style={{ marginTop: -4, marginBottom: 4 }}>{t('profileExtra.preferencesDesc')}</p>
              <Toggle checked={notifyEmail} onChange={setNotifyEmail} label={t('profile.email')} />
              <Toggle checked={notifyWhatsapp} onChange={setNotifyWhatsapp} label={t('profile.whatsappChannel')} />
              <Toggle checked={notifySms} onChange={setNotifySms} label={t('profile.sms')} />
            </fieldset>

            {saved && <p className="alert alert-success" role="status">{t('profileExtra.savedAt')}</p>}
            {save.isError && <p className="alert alert-error" role="alert">{t('errors.generic')}</p>}

            <div className="row" style={{ justifyContent: 'flex-end', gap: 8 }}>
              <Button type="submit" loading={save.isPending} disabled={fullName.trim().length < 2}>
                {t('profileExtra.save')}
              </Button>
            </div>
          </form>
        </section>

        <aside className="card notifications-card">
          <div className="panel-head">
            <h2><Bell size={14} /> {t('profileExtra.notificationsTitle')}</h2>
            {unread > 0 ? <span className="chip chip-gold">{unread}</span> : <span className="chip"><CheckCheck size={11} /> {t('profileExtra.allRead')}</span>}
          </div>
          <p className="panel-body" style={{ color: 'var(--ink-400)', fontSize: 11, paddingBottom: 12 }}>{t('profileExtra.notificationsDesc')}</p>
          {notifications.length === 0 && (
            <div className="empty-state" style={{ padding: '26px 18px' }}>
              <span className="empty-state-icon" aria-hidden="true"><BellRing size={18} /></span>
              <p>{t('profile.noNotifications')}</p>
            </div>
          )}
          {notifications.map((note) => (
            <div key={note.id} className={`notification-row${note.read_at ? '' : ' is-unread'}`}>
              <span className="notification-icon" aria-hidden="true">
                {note.kind.includes('status') ? <MessageCircle size={13} /> : <Bell size={13} />}
              </span>
              <div className="notification-copy">
                <strong>
                  {typeof note.payload.ticket_no === 'string' ? (
                    <span dir="ltr">{note.payload.ticket_no}</span>
                  ) : (
                    note.kind
                  )}
                  {typeof note.payload.subject === 'string' ? ` · ${note.payload.subject}` : ''}
                </strong>
                <span>{formatDate(note.created_at, i18n.language)}</span>
              </div>
              {!note.read_at && (
                <button type="button" className="notification-mark" onClick={() => markRead.mutate(note.id)}>
                  {t('profile.markRead')}
                </button>
              )}
            </div>
          ))}
        </aside>
      </div>

      <section className="card" style={{ marginTop: 18, padding: 20 }}>
        <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
          <div className="row" style={{ gap: 10 }}>
            <span className="notification-icon" aria-hidden="true"><ShieldCheck size={14} /></span>
            <div>
              <strong style={{ display: 'block', fontSize: 12 }}>{t('profileExtra.accountSecurity')}</strong>
              <span style={{ color: 'var(--ink-400)', fontSize: 10.5 }}>{t('profileExtra.accountSecurityDesc')}</span>
            </div>
          </div>
          <span className="chip"><Smartphone size={11} /> {me.mobile}</span>
          <span className="chip"><Mail size={11} /> {me.email ?? '—'}</span>
          <span className="chip"><UserRound size={11} /> {t('profileExtra.verifiedAccount')}</span>
        </div>
      </section>
    </div>
  );
}
