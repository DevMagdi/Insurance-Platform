import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import '../i18n';
import { StatusTimeline } from './StatusTimeline';
import type { TimelineEvent } from '../lib/types';

const events: TimelineEvent[] = [
  { id: 'e1', event_type: 'created', from_status: null, to_status: 'new', occurred_at: '2026-10-01T10:00:00Z' },
  { id: 'e2', event_type: 'status_changed', from_status: 'new', to_status: 'assigned', occurred_at: '2026-10-02T10:00:00Z' },
];

describe('StatusTimeline', () => {
  it('renders the four journey steps with the current status chip (Arabic default)', () => {
    render(<StatusTimeline status="in_review" events={events} />);
    expect(screen.getByText('جديدة')).toBeTruthy();
    expect(screen.getAllByText('قيد المراجعة').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('تم الحل').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('تم إنشاء الطلب')).toBeTruthy();
    expect(screen.getAllByText('تم الإسناد').length).toBeGreaterThanOrEqual(1);
  });

  it('shows the transferred branch instead of pretending resolution', () => {
    render(<StatusTimeline status="transferred" events={events} />);
    expect(screen.getAllByText('محالة لشركة التأمين').length).toBeGreaterThanOrEqual(1);
  });

  it('shows reopened as a loop back to review', () => {
    render(<StatusTimeline status="reopened" events={events} />);
    expect(screen.getAllByText('أُعيد فتحها').length).toBeGreaterThanOrEqual(1);
  });
});
