/**
 * The settings page is the one screen a non-technical operator has to get
 * right on their own, so its shape is pinned: three channel cards, one
 * primary action each, and every vendor detail folded out of the way.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { IntegrationsSettings } from './IntegrationsSettings';

const CONNECTED = {
  id: 'c1', company_id: null, provider: 'whatsapp_cloud', is_enabled: true, has_credentials: true,
  updated_at: '2026-10-18T09:12:00Z', connection_status: 'connected',
  last_test_at: '2026-10-21T07:40:00Z', last_error_code: null,
};
const UNCONFIGURED = {
  id: 'c3', company_id: null, provider: 'imap', is_enabled: false, has_credentials: false,
  updated_at: '2026-09-30T08:00:00Z', connection_status: 'unconfigured',
  last_test_at: null, last_error_code: null,
};

const apiMock = vi.fn();
vi.mock('../../api', () => ({
  api: (...args: unknown[]) => apiMock(...args),
  jsonBody: (body: unknown) => JSON.stringify(body),
}));

const channels = () => [...document.querySelectorAll('.ds-channel')];
const textOf = (el: Element | null | undefined) => el?.textContent?.trim() ?? '';

describe('IntegrationsSettings', () => {
  beforeEach(() => {
    apiMock.mockReset();
    apiMock.mockResolvedValue([CONNECTED, UNCONFIGURED]);
  });

  it('renders exactly three intake channels in a fixed order', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    expect(channels().map((card) => card.getAttribute('data-channel')))
      .toEqual(['whatsapp', 'phone', 'email']);
    expect(channels().map((card) => textOf(card.querySelector('.ds-channel-name'))))
      .toEqual(['واتساب', 'المكالمات الهاتفية', 'البريد الإلكتروني']);
  });

  it('marks a connected channel and offers a connection test', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    const whatsapp = channels()[0];
    expect(textOf(whatsapp.querySelector('.ds-channel-state'))).toBe('متصل');
    expect(whatsapp.classList.contains('is-connected')).toBe(true);
    const labels = [...whatsapp.querySelectorAll('.ds-channel-actions button')].map(textOf);
    expect(labels).toEqual(['إدارة', 'اختبار الاتصال']);
  });

  it('shows an unconnected channel one primary action and no vendor name', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    const email = channels()[2];
    expect(textOf(email.querySelector('.ds-channel-state'))).toBe('غير متصل');
    const actions = [...email.querySelectorAll('.ds-channel-actions button')].map(textOf);
    expect(actions).toEqual(['ربط القناة']);
    // Nothing is stored yet, so the card must not advertise a provider.
    expect(textOf(email).includes('IMAP')).toBe(false);
  });

  it('keeps every technical provider behind a collapsed disclosure', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    const advanced = document.querySelector('.ds-advanced');
    expect(advanced).toBeTruthy();
    expect(advanced?.hasAttribute('open')).toBe(false);
    // The three cards are the only provider UI above the disclosure.
    expect(document.querySelectorAll('.ds-channel-grid .ds-mini-card').length).toBe(0);
  });

  it('asks which provider to use when a channel supports several', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    const connect = channels()[2].querySelector('.ds-channel-actions button') as HTMLButtonElement;
    fireEvent.click(connect);
    await waitFor(() => expect(document.querySelectorAll('.ds-chooser-option').length).toBeGreaterThan(1));
  });

  it('opens the form directly when a channel has a single provider', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    fireEvent.click(channels()[0].querySelector('.ds-channel-actions button') as HTMLButtonElement);
    // WhatsApp has one provider, so no intermediate choice is offered.
    await waitFor(() => expect(document.querySelector('.modal-card')).toBeTruthy());
    expect(document.querySelectorAll('.ds-chooser-option').length).toBe(0);
  });

  it('posts a non-mutating test for the right provider', async () => {
    render(<IntegrationsSettings companies={[]} />);
    await waitFor(() => expect(channels().length).toBe(3));
    apiMock.mockResolvedValueOnce({ connection_status: 'connected', message: 'تم', error_code: null });
    const test = [...channels()[0].querySelectorAll('.ds-channel-actions button')][1] as HTMLButtonElement;
    fireEvent.click(test);
    await waitFor(() => {
      const call = apiMock.mock.calls.find(([url]) => String(url).endsWith('/test'));
      expect(call).toBeTruthy();
      expect(String(call?.[1]?.body)).toContain('whatsapp_cloud');
    });
  });
});
