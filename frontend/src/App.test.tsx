import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import App from './App';

describe('Phase 1 shell', () => {
  it('shows an honest Arabic-first foundation state', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: /إدارة شكاوى التأمين/ })).toBeTruthy();
    expect(screen.getByText(/Phase 1 foundation/)).toBeTruthy();
  });
});
