import { describe, expect, it } from 'vitest';
import {
  accessSpecFromSnapshot,
  catalogSpec,
  hasPermission,
  staffComplaintsQuery,
  type StaffAccess,
} from './staffTypes';

const access: StaffAccess = {
  id: '11111111-1111-4111-8111-111111111111',
  email: 'agent@example.test',
  full_name: 'Agent',
  status: 'active',
  two_factor_enabled: true,
  two_factor_required: false,
  roles: [],
  permissions: ['complaints.view', 'analytics.view'],
  overrides: {},
  all_companies: true,
  company_ids: [],
  insurance_types: [],
  ownership: 'all',
  team_ids: [],
};

describe('staff access helpers', () => {
  it('uses only effective permissions from the backend snapshot', () => {
    expect(hasPermission(access, 'complaints.view')).toBe(true);
    expect(hasPermission(access, 'users.manage_access')).toBe(false);
    expect(hasPermission(null, 'complaints.view')).toBe(false);
  });

  it('converts an access snapshot into the strict access PUT body', () => {
    expect(
      accessSpecFromSnapshot({
        roles: ['role-1'],
        overrides: { 'complaints.view': 'allow', 'complaints.edit': 'deny' },
        all_companies: false,
        company_ids: ['company-1'],
        insurance_types: ['health'],
        ownership: 'team',
        team_ids: ['team-1'],
        email: 'not-a-spec-field@example.test',
      }),
    ).toEqual({
      role_ids: ['role-1'],
      allow: ['complaints.view'],
      deny: ['complaints.edit'],
      all_companies: false,
      company_ids: ['company-1'],
      insurance_types: ['health'],
      ownership: 'team',
      team_ids: ['team-1'],
    });
  });

  it('strips read-only catalog fields before strict saves', () => {
    expect(
      catalogSpec('companies', {
        id: 'company-1',
        slug: 'nile',
        name_en: 'Nile',
        name_ar: 'النيل',
        contact_emails: [],
        supported_types: ['health'],
        is_active: true,
        created_at: '2026-01-01',
      }),
    ).not.toHaveProperty('id');
    expect(catalogSpec('sla-rules', { id: 'sla-1', response_minutes: 60 })).toEqual({
      response_minutes: 60,
      business_hours: {},
    });
  });

  it('serializes queue filters without empty values', () => {
    expect(
      staffComplaintsQuery({ status: 'new', search: 'delayed claim', page: 2, company_id: '' }),
    ).toBe('?status=new&search=delayed+claim&page=2');
    expect(staffComplaintsQuery({})).toBe('');
  });
});
