/**
 * Request archive tests.
 *
 * ARCHIVE and TIMELINE below are copied verbatim from live
 * `/api/corporate/archive` responses (with messages, attachments and a call
 * added by hand, since the dev seed has none), so a change to the server
 * payload breaks this file rather than the browser.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '../../i18n';
import { RequestArchive } from './RequestArchive';
import { formatBytes, formatDuration } from '../../lib/corporate';

const ARCHIVE = {
  items: [
    {
      id: '58db6b39-e1c8-4f51-9348-79dd519b83e1',
      status: 'new',
      channel: 'whatsapp',
      subject: 'طلب موافقة',
      priority: 'normal',
      ticket_no: 'REQ-2026-000002',
      created_at: '2026-10-21T17:09:28.798234+00:00',
      updated_at: '2026-10-21T17:09:28.798234+00:00',
      calls_count: 1,
      card_number: 'CARD-0002',
      employee_id: 'a52ae99c-65c3-4ef5-a3a4-f26a56095736',
      resolved_at: null,
      employee_code: 'E-0002',
      employee_name: 'Karim Nabil',
      intake_source: 'whatsapp',
      policy_number: null,
      messages_count: 2,
      employee_mobile: '+201500000002',
      attachments_count: 3,
    },
    {
      id: '2dc19df8-036c-44dd-84db-b1b168208768',
      status: 'resolved',
      channel: 'phone',
      subject: 'استفسار عن التغطية',
      priority: 'normal',
      ticket_no: 'REQ-2026-000001',
      created_at: '2026-10-21T17:09:28.762022+00:00',
      updated_at: '2026-10-21T17:09:28.762022+00:00',
      calls_count: 0,
      card_number: 'PENDING-REF',
      employee_id: '7b13dc9b-1491-4686-8255-088b88648e21',
      resolved_at: '2026-10-22T09:00:00+00:00',
      employee_code: 'E-0001',
      employee_name: 'سعاد فؤاد',
      intake_source: 'phone',
      policy_number: null,
      messages_count: 0,
      employee_mobile: '+201500000001',
      attachments_count: 0,
    },
  ],
  limit: 25,
  total: 2,
  offset: 0,
  summary: {
    open: 1,
    last_at: '2026-10-21T17:09:28.798234+00:00',
    first_at: '2026-10-21T17:09:28.762022+00:00',
    by_source: { whatsapp: 1, phone: 1 },
    by_status: { new: 1, resolved: 1 },
  },
};

const TIMELINE = {
  id: '58db6b39-e1c8-4f51-9348-79dd519b83e1',
  calls: [
    {
      id: 'd3c5e0fb-6d1a-4c30-9b2f-0b8e1d2a4f77',
      status: 'completed',
      outcome: 'answered',
      channel: null,
      provider: 'twilio',
      recorded: true,
      consented: true,
      direction: 'inbound',
      ended_at: '2026-10-21T17:14:11+00:00',
      started_at: '2026-10-21T17:09:28+00:00',
      duration_seconds: 283,
    },
  ],
  events: [
    {
      to_status: 'new',
      event_type: 'created',
      from_status: null,
      occurred_at: '2026-10-21T17:09:28.798234+00:00',
    },
    {
      to_status: null,
      event_type: 'auto_acknowledged',
      from_status: null,
      occurred_at: '2026-10-21T17:11:02.100000+00:00',
    },
  ],
  status: 'new',
  channel: 'whatsapp',
  subject: 'طلب موافقة',
  employee: {
    id: 'a52ae99c-65c3-4ef5-a3a4-f26a56095736',
    email: null,
    mobile: '+201500000002',
    full_name: 'Karim Nabil',
    employee_code: 'E-0002',
  },
  messages: [
    {
      id: '0f2e6f2d-6f5e-4a67-8a6f-0d3f7d1c1b01',
      body: 'محتاج موافقة على العملية',
      channel: 'whatsapp',
      direction: 'inbound',
      created_at: '2026-10-21T17:09:29+00:00',
      delivery_status: null,
    },
    {
      id: '1a3f7e4c-1c22-4b55-90ab-55f1a9b2cc02',
      body: 'استلمنا طلبك وجارٍ المراجعة.',
      channel: 'whatsapp',
      direction: 'outbound',
      created_at: '2026-10-21T17:11:02+00:00',
      delivery_status: 'sent',
    },
  ],
  priority: 'normal',
  ticket_no: 'REQ-2026-000002',
  created_at: '2026-10-21T17:09:28.798234+00:00',
  updated_at: '2026-10-21T17:09:28.798234+00:00',
  attachments: [
    {
      id: 'b1e2d3c4-5a6b-7c8d-9e0f-112233445566',
      filename: 'approval.pdf',
      mime_type: 'application/pdf',
      size_bytes: 248_000,
      scan_status: 'clean',
      created_at: '2026-10-21T17:09:30+00:00',
    },
  ],
  card_number: 'CARD-0002',
  resolved_at: null,
  intake_source: 'whatsapp',
  policy_number: null,
};

function mockFetch(handler: (url: string) => unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      return {
        ok: true,
        status: 200,
        json: async () => handler(url),
      } as Response;
    }),
  );
}

describe('RequestArchive', () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it('lists every request the company raised, open and closed alike', async () => {
    mockFetch(() => ARCHIVE);
    render(<RequestArchive />);

    await waitFor(() => expect(screen.getByText('REQ-2026-000002')).toBeTruthy());
    // A resolved request is still in the archive: that is the whole point.
    expect(screen.getByText('REQ-2026-000001')).toBeTruthy();
    expect(screen.getByText('Karim Nabil')).toBeTruthy();
    expect(screen.getByText('سعاد فؤاد')).toBeTruthy();
    // Scoped to the table: "تم الحل" is also an option in the status filter.
    const badges = Array.from(document.querySelectorAll('.archive-status')).map(
      (node) => node.textContent,
    );
    expect(badges).toEqual(['جديد', 'تم الحل']);
  });

  it('sends the filters the reader chose, and nothing else', async () => {
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      return ARCHIVE;
    });
    render(<RequestArchive />);
    await waitFor(() => expect(urls.length).toBe(1));

    // An untouched form must not send empty filters.
    expect(urls[0]).not.toContain('search=');
    expect(urls[0]).not.toContain('status=');
    expect(urls[0]).toContain('limit=25');

    fireEvent.change(screen.getByLabelText('بحث'), { target: { value: '01500000002' } });
    fireEvent.change(screen.getByLabelText('الحالة'), { target: { value: 'resolved' } });
    fireEvent.change(screen.getByLabelText('المصدر'), { target: { value: 'phone' } });

    await waitFor(() => {
      const last = urls[urls.length - 1];
      expect(last).toContain('search=01500000002');
      expect(last).toContain('status=resolved');
      expect(last).toContain('source=phone');
    });
  });

  it('searching by mobile number is what the broker asked for', async () => {
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      return ARCHIVE;
    });
    render(<RequestArchive />);
    await waitFor(() => expect(urls.length).toBe(1));

    fireEvent.change(screen.getByLabelText('بحث'), { target: { value: '+20 150 000 0002' } });
    await waitFor(() => {
      // URLSearchParams writes a space as '+' and escapes the leading plus.
      expect(urls[urls.length - 1]).toContain('search=%2B20+150+000+0002');
    });
  });

  it('debounces typing instead of firing a request per keystroke', async () => {
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      return ARCHIVE;
    });
    render(<RequestArchive />);
    await waitFor(() => expect(urls.length).toBe(1));

    const box = screen.getByLabelText('بحث');
    fireEvent.change(box, { target: { value: 'س' } });
    fireEvent.change(box, { target: { value: 'سع' } });
    fireEvent.change(box, { target: { value: 'سعا' } });
    fireEvent.change(box, { target: { value: 'سعاد' } });

    await waitFor(() => expect(urls[urls.length - 1]).toContain(encodeURIComponent('سعاد')));
    // Four keystrokes, one extra request — not four.
    expect(urls.length).toBe(2);
  });

  it('clears every filter at once', async () => {
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      return ARCHIVE;
    });
    render(<RequestArchive />);
    await waitFor(() => expect(urls.length).toBe(1));

    fireEvent.change(screen.getByLabelText('المصدر'), { target: { value: 'phone' } });
    await waitFor(() => expect(screen.getByText('مسح عوامل التصفية')).toBeTruthy());

    fireEvent.click(screen.getByText('مسح عوامل التصفية'));
    await waitFor(() => expect(urls[urls.length - 1]).not.toContain('source='));
  });

  it('reports the filtered totals, not the page', async () => {
    mockFetch(() => ARCHIVE);
    render(<RequestArchive />);

    const summary = await screen.findByLabelText('ملخص النتائج');
    expect(summary.textContent).toContain('إجمالي النتائج');
    expect(summary.textContent).toContain('المفتوحة');
    // total=2 with one open: the summary describes the filtered set.
    expect(summary.textContent).toContain('2');
    expect(summary.textContent).toContain('1');
  });

  it('opens one request into its messages, attachments and calls', async () => {
    mockFetch((url) => (url.includes('/archive/') ? TIMELINE : ARCHIVE));
    render(<RequestArchive />);

    await waitFor(() => expect(screen.getByText('REQ-2026-000002')).toBeTruthy());
    fireEvent.click(screen.getAllByText('عرض')[0]);

    await waitFor(() => expect(screen.getByLabelText('تفاصيل الطلب')).toBeTruthy());
    expect(screen.getByText('محتاج موافقة على العملية')).toBeTruthy();
    expect(screen.getByText('استلمنا طلبك وجارٍ المراجعة.')).toBeTruthy();
    expect(screen.getByText('approval.pdf')).toBeTruthy();
    // The automation leaves a visible trace in the company's own record.
    expect(screen.getByText('تم إرسال إشعار الاستلام تلقائياً')).toBeTruthy();
  });

  it('shows that a call happened without offering the recording', async () => {
    mockFetch((url) => (url.includes('/archive/') ? TIMELINE : ARCHIVE));
    render(<RequestArchive />);

    await waitFor(() => expect(screen.getByText('REQ-2026-000002')).toBeTruthy());
    fireEvent.click(screen.getAllByText('عرض')[0]);
    await waitFor(() => expect(screen.getByLabelText('تفاصيل الطلب')).toBeTruthy());

    expect(screen.getByText(/مكالمة واردة/)).toBeTruthy();
    expect(screen.getByText(/مسجّلة/)).toBeTruthy();
    // Metadata only: no audio element, no download link for the recording.
    expect(document.querySelector('audio')).toBeNull();
    expect(screen.queryByText('تحميل التسجيل')).toBeNull();
  });

  it('names the reference a request was opened against', async () => {
    mockFetch(() => ARCHIVE);
    render(<RequestArchive />);
    await waitFor(() => expect(screen.getByText('REQ-2026-000001')).toBeTruthy());

    mockFetch((url) =>
      url.includes('/archive/')
        ? { ...TIMELINE, card_number: 'PENDING-REF', policy_number: null }
        : ARCHIVE,
    );
    fireEvent.click(screen.getAllByText('عرض')[0]);

    // "PENDING-REF" is a placeholder the intake writes when neither number
    // arrived; showing it raw would read as a real card number.
    await waitFor(() =>
      expect(screen.getByText('بانتظار رقم الوثيقة أو الكارنيه')).toBeTruthy(),
    );
    expect(screen.queryByText(/PENDING-REF/)).toBeNull();
  });

  it('says so plainly when nothing matches', async () => {
    mockFetch(() => ({ ...ARCHIVE, items: [], total: 0, summary: { ...ARCHIVE.summary, open: 0 } }));
    render(<RequestArchive />);
    await waitFor(() => expect(screen.getByText('لا توجد طلبات مطابقة')).toBeTruthy());
  });

  it('hides the pager until there is a second page', async () => {
    mockFetch(() => ARCHIVE);
    render(<RequestArchive />);
    await waitFor(() => expect(screen.getByText('REQ-2026-000002')).toBeTruthy());
    expect(screen.queryByText('التالي')).toBeNull();
  });

  it('pages forward and returns to the first page when a filter changes', async () => {
    const urls: string[] = [];
    mockFetch((url) => {
      urls.push(url);
      return { ...ARCHIVE, total: 60 };
    });
    render(<RequestArchive />);
    await waitFor(() => expect(screen.getByText('التالي')).toBeTruthy());

    fireEvent.click(screen.getByText('التالي'));
    await waitFor(() => expect(urls[urls.length - 1]).toContain('offset=25'));

    fireEvent.change(screen.getByLabelText('الحالة'), { target: { value: 'new' } });
    await waitFor(() => {
      const last = urls[urls.length - 1];
      expect(last).toContain('offset=0');
      expect(last).toContain('status=new');
    });
  });

  it('surfaces a failure instead of an empty table', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 500,
        json: async () => ({ detail: 'boom' }),
      })),
    );
    render(<RequestArchive />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
  });
});

describe('archive formatting', () => {
  it('reads a call length as minutes and seconds', () => {
    expect(formatDuration(283)).toBe('٤:43 دقيقة');
    expect(formatDuration(45)).toBe('٤٥ ثانية');
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(-1)).toBe('—');
  });

  it('reads an attachment size in the unit a person would use', () => {
    expect(formatBytes(512)).toBe('٥١٢ بايت');
    expect(formatBytes(248_000)).toBe('٢٤٢ ك.ب');
    expect(formatBytes(5_400_000)).toBe('٥٫١ م.ب');
    expect(formatBytes(null)).toBe('—');
  });
});
