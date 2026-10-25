/**
 * The two blocks a shift lead actually opens the dashboard for: where today's
 * requests came in from, and what has just happened.
 *
 * Both read live endpoints that already exist — the intake split is three
 * counted queries against the same list the Requests screen uses, so a number
 * here and a number there can never disagree.
 */
import { useEffect, useMemo, useState } from 'react';
import { api, queryString } from '../../api';
import { Icon, type IconName } from '../../components/Icon';
import { Panel, PanelHeader, StatusPill, Blank, SkeletonRows } from '../../components/ui/Premium';
import { formatNumber } from '../../utils/date';
import type { Complaint, ComplaintListResponse } from '../../types';

/* ------------------------------------------------------------ intake split */

interface SourceSpec {
  id: string;
  label: string;
  icon: IconName;
  color: string;
  tint: string;
}

/**
 * Each channel carries its own hue so the three bars stay tellable apart at a
 * glance. None of them is the brand gold: these are data, and the accent is
 * reserved for actions and the current location. Email was amber and read as
 * "the important one" purely because it matched the primary button, so it
 * moves to the same dusty rose as the transferred status.
 */
const SOURCES: SourceSpec[] = [
  { id: 'whatsapp', label: 'واتساب', icon: 'message', color: '#128C7E', tint: 'rgba(18, 140, 126, 0.13)' },
  { id: 'phone', label: 'مكالمات', icon: 'phone', color: '#4A86D9', tint: 'rgba(74, 134, 217, 0.13)' },
  { id: 'email', label: 'بريد إلكتروني', icon: 'mail', color: '#C46A85', tint: 'rgba(196, 106, 133, 0.13)' },
];

export function IntakeBreakdown({ filterQuery, onPick }: { filterQuery: string; onPick?: (source: string) => void }) {
  const [counts, setCounts] = useState<Record<string, number> | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    const run = async () => {
      try {
        const results = await Promise.all(
          SOURCES.map(async (source) => {
            const query = queryString({ intake_source: source.id, page_size: 1 });
            const merged = filterQuery ? `${filterQuery}&${query.slice(1)}` : query;
            const data = await api<ComplaintListResponse>(`/api/staff/complaints${merged}`);
            return [source.id, data.total ?? 0] as const;
          }),
        );
        if (active) { setCounts(Object.fromEntries(results)); setFailed(false); }
      } catch {
        if (active) setFailed(true);
      }
    };
    void run();
    return () => { active = false; };
  }, [filterQuery]);

  const total = useMemo(
    () => (counts ? Object.values(counts).reduce((sum, value) => sum + value, 0) : 0),
    [counts],
  );

  return (
    <Panel className="ds-intake">
      <PanelHeader
        title="مصادر الطلبات"
        subtitle="من أين وصلت الطلبات خلال الفترة المحددة"
      />
      {failed ? (
        <Blank title="تعذر حساب المصادر" body="حدّث الصفحة للمحاولة مرة أخرى." />
      ) : !counts ? (
        <SkeletonRows rows={3} />
      ) : total === 0 ? (
        <Blank icon={<Icon name="inbox" size={22} />} title="لا توجد طلبات في هذه الفترة" />
      ) : (
        <ul className="ds-intake-list">
          {SOURCES.map((source) => {
            const value = counts[source.id] ?? 0;
            const share = total ? Math.round((value / total) * 100) : 0;
            return (
              <li key={source.id}>
                <button
                  type="button"
                  className="ds-intake-row"
                  onClick={() => onPick?.(source.id)}
                  disabled={!onPick}
                >
                  <span className="ds-intake-icon" style={{ color: source.color, background: source.tint }}>
                    <Icon name={source.icon} size={17} />
                  </span>
                  <span className="ds-intake-label">{source.label}</span>
                  <span className="ds-intake-count ds-metric">{formatNumber(value)}</span>
                  <span className="ds-intake-share">{formatNumber(share)}٪</span>
                  <span className="ds-intake-track" aria-hidden="true">
                    <span
                      className="ds-intake-fill"
                      style={{ width: `${share}%`, background: source.color }}
                    />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/* --------------------------------------------------------------- activity */

const CHANNEL_ICON: Record<string, IconName> = {
  whatsapp: 'message',
  phone: 'phone',
  ai_voice: 'phone',
  email: 'mail',
  sms: 'message',
  portal: 'globe',
};

/** Relative time, in Arabic, without pulling in a date library. */
function sinceLabel(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diff / 60000);
  if (!Number.isFinite(minutes)) return '';
  if (minutes < 1) return 'الآن';
  if (minutes < 60) return `منذ ${formatNumber(minutes)} دقيقة`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `منذ ${formatNumber(hours)} ساعة`;
  return `منذ ${formatNumber(Math.round(hours / 24))} يوم`;
}

const STATUS_LABEL: Record<string, string> = {
  new: 'جديد',
  assigned: 'مُسند',
  in_review: 'قيد المراجعة',
  resolved: 'تم الحل',
  reopened: 'أُعيد فتحه',
  transferred: 'محوّل',
};

export function RecentActivity({ onOpen }: { onOpen?: (id: string) => void }) {
  const [items, setItems] = useState<Complaint[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    const run = async () => {
      try {
        const data = await api<ComplaintListResponse>(
          `/api/staff/complaints${queryString({ page_size: 6, sort: '-updated_at' })}`,
        );
        if (active) { setItems(data.items ?? []); setFailed(false); }
      } catch {
        if (active) setFailed(true);
      }
    };
    void run();
    const interval = window.setInterval(() => { void run(); }, 30_000);
    return () => { active = false; window.clearInterval(interval); };
  }, []);

  return (
    <Panel className="ds-activity">
      <PanelHeader title="آخر النشاط" subtitle="أحدث الطلبات والتحديثات" />
      {failed ? (
        <Blank title="تعذر تحميل النشاط" />
      ) : !items ? (
        <SkeletonRows rows={5} />
      ) : items.length === 0 ? (
        <Blank icon={<Icon name="inbox" size={22} />} title="لا يوجد نشاط بعد" />
      ) : (
        <ol className="ds-feed">
          {items.map((item) => (
            <li key={item.id}>
              <button type="button" className="ds-feed-row" onClick={() => onOpen?.(item.id)} disabled={!onOpen}>
                <span className="ds-feed-rail" aria-hidden="true">
                  <span className="ds-feed-node" data-tone={item.status}>
                    <Icon name={CHANNEL_ICON[item.channel] ?? 'inbox'} size={13} />
                  </span>
                </span>
                <span className="ds-feed-body">
                  <span className="ds-feed-top">
                    <strong className="ds-feed-subject">{item.subject || 'طلب بدون عنوان'}</strong>
                    <StatusPill tone={(item.status as never) ?? 'neutral'}>
                      {STATUS_LABEL[item.status] ?? item.status}
                    </StatusPill>
                  </span>
                  <span className="ds-feed-meta">
                    <span className="ds-mono">{item.ticket_no}</span>
                    <i aria-hidden="true">·</i>
                    <span>{item.customer_name || 'عميل غير مُعرّف'}</span>
                    <i aria-hidden="true">·</i>
                    <span>{sinceLabel(item.updated_at || item.created_at)}</span>
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

/* ---------------------------------------------------------- quick actions */

export function QuickActions({ actions }: { actions: Array<{ label: string; hint: string; icon: IconName; onClick: () => void; primary?: boolean }> }) {
  return (
    <div className="ds-quick-grid">
      {actions.map((action) => (
        <button
          key={action.label}
          type="button"
          className={`ds-quick${action.primary ? ' is-primary' : ''}`}
          onClick={action.onClick}
        >
          <span className="ds-quick-icon"><Icon name={action.icon} size={18} /></span>
          <span className="ds-quick-text">
            <strong>{action.label}</strong>
            <small>{action.hint}</small>
          </span>
          <Icon name="arrow" size={15} />
        </button>
      ))}
    </div>
  );
}
