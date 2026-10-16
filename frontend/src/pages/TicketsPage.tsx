import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../lib/api';
import { navigate } from '../lib/router';
import { formatDate, STATUS_META } from '../lib/status';
import type { ComplaintFilters, ComplaintStatus } from '../lib/types';
import { useMe } from '../hooks/useMe';
import { EmptyState, ErrorState, LoadingState, SelectInput, StatusChip, TextInput, primaryButton } from '../components/ui';
import { LoginPage } from './LoginPage';

const PAGE_SIZE = 10;
const STATUS_FILTERS: (ComplaintStatus | '')[] = ['', 'new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred'];

export function TicketsPage() {
  const { t, i18n } = useTranslation();
  const { me, isLoading: meLoading } = useMe();
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<ComplaintStatus | ''>('');
  const [companyId, setCompanyId] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [page, setPage] = useState(1);

  const filters: ComplaintFilters = useMemo(() => {
    const base: ComplaintFilters = {
      page,
      page_size: PAGE_SIZE,
      status: status || undefined,
      company_id: companyId || undefined,
      date_from: dateFrom ? new Date(`${dateFrom}T00:00:00.000Z`).toISOString() : undefined,
      date_to: dateTo ? new Date(`${dateTo}T23:59:59.999Z`).toISOString() : undefined,
    };
    const term = search.trim();
    if (/^CMP-/i.test(term)) base.ticket_no = term.toUpperCase();
    else if (term.length >= 2) base.search = term;
    return base;
  }, [search, status, companyId, dateFrom, dateTo, page]);

  const enabled = Boolean(me);
  const listQuery = useQuery({
    queryKey: ['my-complaints', filters],
    queryFn: () => api.listComplaints(filters),
    enabled,
  });
  const countersQuery = useQuery({
    queryKey: ['my-counters', { ...filters, status: undefined, page: 1 }],
    queryFn: () => api.counters({ ...filters, status: undefined, page: 1 }),
    enabled,
  });
  const companiesQuery = useQuery({ queryKey: ['companies'], queryFn: api.companies, staleTime: 300_000 });

  if (meLoading) return <LoadingState />;
  if (!me) return <LoginPage />;

  const counters = countersQuery.data;
  const totalPages = Math.max(1, Math.ceil((listQuery.data?.total ?? 0) / PAGE_SIZE));
  const companyName = (id: string) => {
    const company = companiesQuery.data?.find((c) => c.id === id);
    return company ? (i18n.language === 'ar' ? company.name_ar : company.name_en) : '';
  };

  const counterCards = counters
    ? [
        { label: t('tickets.total'), value: counters.total, testId: 'counter-total' },
        { label: t('tickets.open'), value: counters.open, testId: 'counter-open' },
        { label: t('tickets.inReview'), value: counters.in_review, testId: 'counter-in-review' },
        { label: t('tickets.resolved'), value: counters.closed, testId: 'counter-resolved' },
        { label: t('tickets.transferred'), value: counters.transferred, testId: 'counter-transferred' },
      ]
    : [];

  return (
    <section>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{t('tickets.title')}</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">{t('tickets.subtitle')}</p>
        </div>
        <button type="button" className={primaryButton} onClick={() => navigate('/complaints/new')}>
          {t('tickets.newComplaint')}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5" aria-label="counters">
        {counterCards.map((card) => (
          <div key={card.testId} className="rounded-2xl border border-slate-200/80 bg-white/70 p-4 backdrop-blur dark:border-white/10 dark:bg-white/5">
            <p className="text-2xl font-black" data-testid={card.testId}>{card.value}</p>
            <p className="text-xs text-slate-500 dark:text-slate-400">{card.label}</p>
          </div>
        ))}
      </div>

      <div className="mt-5 rounded-3xl border border-slate-200/80 bg-white/70 p-4 backdrop-blur dark:border-white/10 dark:bg-white/5">
        <div className="grid gap-3 md:grid-cols-[1fr_auto_auto_auto]">
          <TextInput
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            placeholder={t('tickets.searchPlaceholder')}
            aria-label={t('common.search')}
          />
          <SelectInput
            value={companyId}
            onChange={(e) => {
              setCompanyId(e.target.value);
              setPage(1);
            }}
            aria-label={t('wizard.company')}
          >
            <option value="">{t('wizard.company')}</option>
            {(companiesQuery.data ?? []).map((company) => (
              <option key={company.id} value={company.id}>
                {i18n.language === 'ar' ? company.name_ar : company.name_en}
              </option>
            ))}
          </SelectInput>
          <TextInput type="date" value={dateFrom} onChange={(e) => { setDateFrom(e.target.value); setPage(1); }} aria-label={t('tickets.from')} />
          <TextInput type="date" value={dateTo} onChange={(e) => { setDateTo(e.target.value); setPage(1); }} aria-label={t('tickets.to')} />
        </div>
        <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label={t('track.status')}>
          {STATUS_FILTERS.map((value) => (
            <button
              key={value || 'all'}
              type="button"
              onClick={() => {
                setStatus(value);
                setPage(1);
              }}
              className={`rounded-full px-3 py-1.5 text-xs font-semibold ring-1 transition ${
                status === value
                  ? 'bg-amber-400 text-slate-950 ring-amber-400'
                  : 'bg-transparent ring-slate-300/70 hover:bg-slate-900/5 dark:ring-white/15 dark:hover:bg-white/10'
              }`}
            >
              {value ? t(STATUS_META[value].labelKey) : t('status.all')}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {listQuery.isLoading && <LoadingState />}
        {listQuery.isError && <ErrorState message={t('errors.generic')} onRetry={() => void listQuery.refetch()} />}
        {listQuery.data?.items.map((ticket) => (
          <button
            key={ticket.id}
            type="button"
            onClick={() => navigate(`/tickets/${ticket.id}`)}
            className="block w-full rounded-3xl border border-slate-200/80 bg-white/70 p-4 text-start shadow-sm backdrop-blur transition hover:border-amber-300 dark:border-white/10 dark:bg-white/5"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-sm font-bold" dir="ltr">{ticket.ticket_no}</span>
              <StatusChip status={ticket.status} />
            </div>
            <p className="mt-1.5 font-medium">{ticket.subject}</p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
              {companyName(ticket.company_id)} · {t(`types.${ticket.insurance_type}`)} · {formatDate(ticket.created_at, i18n.language)}
            </p>
          </button>
        ))}
        {listQuery.data && listQuery.data.items.length === 0 && <EmptyState message={t('tickets.empty')} />}
      </div>

      {totalPages > 1 && (
        <div className="mt-5 flex items-center justify-center gap-3">
          <button type="button" className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40 dark:border-white/15" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            {t('tickets.prev')}
          </button>
          <span className="text-sm text-slate-500">{t('tickets.page', { page, pages: totalPages })}</span>
          <button type="button" className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40 dark:border-white/15" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            {t('tickets.nextPage')}
          </button>
        </div>
      )}
    </section>
  );
}
