import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import App from './App';

describe('Phase 5 app shell', () => {
  it('renders the Arabic-first product landing page and complaint CTAs', async () => {
    render(<App />);
    expect(await screen.findByRole('heading', { name: /إدارة شكاوى التأمين/ })).toBeTruthy();
    expect(screen.getByRole('link', { name: /تقديم شكوى/ })).toBeTruthy();
    expect(screen.getAllByRole('link', { name: /تتبع شكوى/ }).length).toBeGreaterThan(0);
  });
});
