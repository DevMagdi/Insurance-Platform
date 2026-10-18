import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import '../i18n';
import { NewComplaintPage } from './NewComplaintPage';

const COMPANIES = [
  { id: '11111111-1111-4111-8111-111111111111', name_en: 'Nile Insurance', name_ar: 'النيل للتأمين', insurance_types: ['health', 'general', 'both'] },
];

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/me')) return new Response('{"detail":"unauthorized"}', { status: 401 });
    if (url.includes('/api/companies')) return Response.json(COMPANIES);
    return new Response('{}', { status: 404 });
  });
}

function renderWizard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NewComplaintPage />
    </QueryClientProvider>,
  );
}

describe('complaint wizard', () => {
  it('validates identity live and blocks invalid national IDs', async () => {
    vi.stubGlobal('fetch', mockFetch());
    renderWizard();
    fireEvent.click(screen.getByText('التالي'));
    await waitFor(() => expect(screen.getByText(/رقم قومي غير صحيح/)).toBeTruthy());
    expect(screen.getByText(/رقم موبايل مصري/)).toBeTruthy();
  });

  it('walks all steps, autosaves the draft and reaches the review step', async () => {
    vi.stubGlobal('fetch', mockFetch());
    renderWizard();

    fireEvent.change(screen.getByLabelText(/الاسم الكامل/), { target: { value: 'Mona Ali' } });
    fireEvent.change(screen.getByLabelText(/الرقم القومي/), { target: { value: '29901010112345' } });
    fireEvent.change(screen.getByLabelText(/رقم الموبايل/), { target: { value: '01123456789' } });
    fireEvent.click(screen.getByText('التالي'));

    await waitFor(() =>
      expect((screen.getByLabelText(/شركة التأمين/) as HTMLSelectElement).options.length).toBeGreaterThan(1),
    );
    fireEvent.change(screen.getByLabelText(/شركة التأمين/), { target: { value: COMPANIES[0].id } });
    fireEvent.click(screen.getByRole('radio', { name: 'تأمين صحي' }));
    fireEvent.change(screen.getByLabelText(/رقم البوليصة/), { target: { value: 'POL-000123' } });
    fireEvent.click(screen.getByText('التالي'));

    await waitFor(() => expect(screen.getByLabelText(/عنوان الشكوى/)).toBeTruthy());
    fireEvent.change(screen.getByLabelText(/عنوان الشكوى/), { target: { value: 'تأخر صرف التعويض' } });
    fireEvent.change(screen.getByLabelText(/تفاصيل الشكوى/), {
      target: { value: 'تأخر صرف تعويض العملية الجراحية منذ أكثر من شهرين دون رد واضح.' },
    });
    fireEvent.click(screen.getByText('التالي'));

    await waitFor(() => expect(screen.getByText(/اسحب الملفات هنا/)).toBeTruthy());
    fireEvent.click(screen.getByText('التالي'));

    await waitFor(() => expect(screen.getByText('راجع بياناتك قبل الإرسال')).toBeTruthy());
    expect(screen.getByText('Mona Ali')).toBeTruthy();
    expect(screen.getByText('•••• •••• ••23 45')).toBeTruthy(); // national ID stays masked
    expect(screen.getByText('+201123456789')).toBeTruthy();

    // PDPL: submission is blocked until explicit consent is given.
    fireEvent.click(screen.getByText('إرسال الشكوى الآن'));
    await waitFor(() => expect(screen.getByText(/يلزم الموافقة على معالجة البيانات/)).toBeTruthy());
    expect(screen.getByTestId('privacy-consent')).toBeTruthy();

    await waitFor(() => {
      const raw = localStorage.getItem('insurance-portal:complaint-draft:v1');
      expect(raw).toBeTruthy();
      expect(raw).not.toContain('29901010112345');
      expect(raw).not.toContain('Mona Ali');
      expect(raw).not.toContain('تأخر صرف تعويض');
      expect(JSON.parse(raw as string).national_id).toBeUndefined();
    });
  });
});
