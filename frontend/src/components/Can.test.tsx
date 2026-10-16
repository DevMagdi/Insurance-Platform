import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { Can } from './Can';
import { useAuth } from '../store/auth';

describe('Can permission gate', () => {
  beforeEach(() => useAuth.setState({ kind: 'staff', permissions: ['complaints.view'] }));
  it('shows only content authorized by effective permissions', () => {
    render(<><Can permission="complaints.view"><span>Visible</span></Can><Can permission="users.view"><span>Hidden</span></Can></>);
    expect(screen.getByText('Visible')).toBeTruthy();
    expect(screen.queryByText('Hidden')).toBeNull();
  });
  it('denies permissions to customers', () => {
    useAuth.setState({ kind: 'customer' });
    render(<Can permission="complaints.view"><span>Staff action</span></Can>);
    expect(screen.queryByText('Staff action')).toBeNull();
  });
});
