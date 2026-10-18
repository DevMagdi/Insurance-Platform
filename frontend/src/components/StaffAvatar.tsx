import { useState } from 'react';

interface Props {
  name: string;
  src?: string | null;
  status?: string;
}

/** Private same-origin images use the existing HttpOnly session cookies. */
export function StaffAvatar({ name, src, status = 'active' }: Props) {
  const [failed, setFailed] = useState(false);
  const initials = (name || 'موظف').split(' ').filter(Boolean).map((part) => part[0]).slice(0, 2).join('');
  return <span className={`avatar-circle avatar-${status}`}>
    {src?.startsWith('/api/staff/users/') && !failed
      ? <img src={src} alt={`صورة ${name}`} loading="lazy" onError={() => setFailed(true)} style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 'inherit' }} />
      : initials}
  </span>;
}
