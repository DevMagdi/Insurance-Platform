import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { api, queryString } from '../../api';
import { ScopeBadge } from '../../components/Access';
import { Icon, type IconName } from '../../components/Icon';
import { hasPermission } from '../../utils/access';
import { formatNumber, rangeBounds } from '../../utils/date';
import { SECTION_DESCRIPTION, SECTION_GLOW } from './sections';
import type { AnalyticsOverview, AppRoute, CompanyOption, DateRange, StaffAccess } from '../../types';

export interface LaunchpadItem {
  page: AppRoute['page'];
  label: string;
  icon: IconName;
  section: 'workspace' | 'more';
}

interface LaunchpadProps {
  access: StaffAccess;
  range: DateRange;
  items: LaunchpadItem[];
  companies: CompanyOption[];
  onNavigate: (route: AppRoute) => void;
}

interface CrmStatsLite {
  total_leads: number;
  conversion_rate: number;
}

/**
 * Card OS home — the console's primary navigation.
 *
 * A Bento of large, softly glowing section cards replaces the sidebar:
 * the day's work first, the CRM as the big coral card, governance behind
 * it. Cards are built from the same permission-filtered navigation the
 * shell always computed, so an operator only ever sees sections they
 * may open; quick values ride the existing analytics/CRM stats APIs
 * and simply stay empty when those fail or are not permitted.
 */
export function Launchpad({ access, range, items, companies, onNavigate }: LaunchpadProps) {
  const [analytics, setAnalytics] = useState<AnalyticsOverview | null>(null);
  const [crmStats, setCrmStats] = useState<CrmStatsLite | null>(null);
  const bounds = useMemo(() => rangeBounds(range), [range]);
  const canAnalytics = hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view');
  const canCrm = hasPermission(access, 'crm.view');

  useEffect(() => {
    let active = true;
    if (canAnalytics) {
      void api<AnalyticsOverview>(`/api/staff/analytics/overview${queryString({ ...bounds })}`)
        .then((result) => { if (active) setAnalytics(result); })
        .catch(() => { /* cards keep their descriptive line */ });
    }
    if (canCrm) {
      void api<CrmStatsLite>('/api/crm/stats')
        .then((result) => { if (active) setCrmStats(result); })
        .catch(() => { /* cards keep their descriptive line */ });
    }
    return () => { active = false; };
  }, [bounds, canAnalytics, canCrm]);

  const valueFor = (page: AppRoute['page']): string | null => {
    if (page === 'complaints' && analytics) {
      const open = analytics.kpis.new + analytics.kpis.assigned + analytics.kpis.in_review + analytics.kpis.reopened;
      return `${formatNumber(open)} طلب مفتوح في الفترة`;
    }
    if (page === 'overview' && analytics) {
      return `${formatNumber(analytics.kpis.total)} طلب · ${formatNumber(analytics.kpis.sla_breaches)} تجاوز SLA`;
    }
    if (page === 'crm-module' && crmStats) {
      return `${formatNumber(crmStats.total_leads)} lead · تحويل ${formatNumber(crmStats.conversion_rate)}٪`;
    }
    return null;
  };

  const workspace = items.filter((item) => item.section === 'workspace');
  const more = items.filter((item) => item.section === 'more');
  const firstName = access.full_name.split(/\s+/)[0] ?? access.full_name;
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'صباح الخير' : 'مساء الخير';
  const today = new Intl.DateTimeFormat('ar-EG', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date());

  const card = (item: LaunchpadItem, index: number, wide = false) => {
    const value = valueFor(item.page);
    return (
      <button
        type="button"
        key={item.page}
        className={`os-card${wide ? ' os-card-wide' : ''}`}
        data-glow={SECTION_GLOW[item.page] ?? 'admin'}
        style={{ '--os-i': index } as CSSProperties}
        onClick={() => onNavigate(
          item.page === 'crm-module' ? { page: 'crm-module', tab: 'dashboard' } : ({ page: item.page } as AppRoute),
        )}
      >
        <span className="os-card-icon"><Icon name={item.icon} size={wide ? 30 : 24} /></span>
        <span className="os-card-body">
          <strong>{item.label}</strong>
          {value && <span className="os-card-value">{value}</span>}
          <small>{SECTION_DESCRIPTION[item.page]}</small>
        </span>
        <span className="os-card-arrow"><Icon name="arrow" size={16} /></span>
      </button>
    );
  };

  const crmItem: LaunchpadItem = { page: 'crm-module', label: 'CRM', icon: 'users', section: 'workspace' };
  let index = 0;

  return (
    <div className="os-launchpad">
      <header className="os-hero">
        <div>
          <p className="os-hero-eyebrow">{today}</p>
          <h1>{greeting}، {firstName}</h1>
          <p className="os-hero-sub">اختر قسمًا للمتابعة — كل أقسام المنصة أمامك ببطاقات مباشرة.</p>
        </div>
        <div className="os-hero-scope">
          <span className="os-hero-scope-label"><Icon name="shield" size={14} /> النطاق الفعّال</span>
          <ScopeBadge access={access} companies={companies} />
        </div>
      </header>

      <nav className="os-bento" aria-label="أقسام المنصة">
        {workspace.map((item) => card(item, index++, item.page === 'complaints'))}
        {canCrm && card(crmItem, index++, true)}
      </nav>

      {more.length > 0 && (
        <>
          <h2 className="os-section-title">الإدارة والمزيد</h2>
          <nav className="os-bento os-bento-more" aria-label="أقسام الإدارة">
            {more.map((item) => card(item, index++))}
          </nav>
        </>
      )}
    </div>
  );
}
