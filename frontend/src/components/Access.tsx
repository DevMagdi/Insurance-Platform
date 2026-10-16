import type { ReactNode } from 'react';
import type { CompanyOption, StaffAccess } from '../types';
import { Icon } from './Icon';
import { hasPermission } from '../utils/access';

interface ScopeBadgeProps {
  access: StaffAccess;
  companies: CompanyOption[];
}

export function ScopeBadge({ access, companies }: ScopeBadgeProps) {
  const scopedNames = access.all_companies
    ? []
    : access.company_ids.map((id) => companies.find((company) => company.id === id)?.name_ar ?? id.slice(0, 8));
  const label = access.all_companies
    ? 'جميع الشركات'
    : scopedNames.length > 0
      ? scopedNames.length > 2
        ? `${scopedNames.slice(0, 2).join('، ')} +${scopedNames.length - 2}`
        : scopedNames.join('، ')
      : `${access.company_ids.length} شركة ضمن النطاق`;
  const typeLabel = access.insurance_types.length === 0
    ? 'جميع أنواع التأمين'
    : access.insurance_types.map((type) => ({ health: 'طبي', general: 'عام', both: 'مشترك' })[type]).join('، ');
  return (
    <div className="scope-badge" title={`${label} · ${typeLabel} · ${access.ownership}`}>
      <span className="scope-badge-icon"><Icon name="shield" size={15} /></span>
      <span className="scope-badge-text"><strong>{label}</strong><small>{typeLabel}</small></span>
      <span className="scope-live-dot" aria-label="نطاق مفعّل" />
    </div>
  );
}

export function AccessDenied({ title = 'لا تملك صلاحية الوصول', children }: { title?: string; children?: ReactNode }) {
  return (
    <div className="access-denied" role="status">
      <span className="empty-icon"><Icon name="lock" size={22} /></span>
      <h3>{title}</h3>
      <p>{children ?? 'تم إخفاء هذه المساحة لأن صلاحيتك الحالية لا تسمح بعرضها.'}</p>
    </div>
  );
}

export function PermissionOnly({ access, code, children }: { access: StaffAccess; code: string; children: ReactNode }) {
  return hasPermission(access, code) ? <>{children}</> : null;
}
