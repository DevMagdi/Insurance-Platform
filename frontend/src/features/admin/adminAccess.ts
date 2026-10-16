import { hasPermission } from '../../utils/access';
import type { StaffAccess } from '../../types';

export function canViewAdminDashboard(access: StaffAccess): boolean {
  return access.all_companies && access.company_ids.length === 0 && access.insurance_types.length === 0
    && access.ownership === 'all' && hasPermission(access, 'analytics.view') && hasPermission(access, 'complaints.view');
}
