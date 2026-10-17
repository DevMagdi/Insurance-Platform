import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ArrowDownUp, ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, Filter, Plus, SearchX, SlidersHorizontal } from 'lucide-react';
import { api } from '../lib/api';
import { navigate } from '../lib/router';
import { formatDate, STATUS_META } from '../lib/status';
import type { ComplaintFilters, ComplaintStatus } from '../lib/types';
import { useMe } from '../hooks/useMe';
import { Button, EmptyState, ErrorState, LoadingState, SearchInput, SelectInput, StatusChip } from '../components/ui';
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
  const [filtersOpen, setFiltersOpen] = useState(false);

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
  const Arrow = i18n.language === 'ar' ? ArrowLeft : ArrowRight;
  const Prev = i18n.language === 'ar' ? ChevronRight : ChevronLeft;
  const Next = i18n.language === 'ar' ? ChevronLeft : ChevronRight;

  const counterCards = counters
    ? [
        { label: t('tickets.total'), value: counters.total, testId: 'counter-total', tone: 'neutral' as const },
        { label: t('tickets.open'), value: counters.open, testId: 'counter-open', tone: 'gold' as const },
        { label: t('tickets.inReview'), value: counters.in_review, testId: 'counter-in-review', tone: 'info' as const },
        { label: t('tickets.resolved'), value: counters.closed, testId: 'counter-resolved', tone: 'success' as const },
        { label: t('tickets.transferred'), value: counters.transferred, testId: 'counter-transferred', tone: 'neutral' as const },
      ]
    : [];
  const activeFilterCount = [status, companyId, dateFrom, dateTo].filter(Boolean).length;
  const resetFilters = () => {
    setStatus('');
    setCompanyId('');
    setDateFrom('');
    setDateTo('');
    setSearch('');
    setPage(1);
  };

  return (
    <div>
      <div className="page-intro">
        <div>
          <span className="eyebrow">{t('home.badge')}</span>
          <h1>{t('tickets.title')}</h1>
          <p>{t('tickets.subtitle')}</p>
        </div>
        <div className="page-intro-actions">
          <Button onClick={() => navigate('/complaints/new')} leadingIcon={<Plus size={15} />}>
            {t('tickets.newComplaint')}
          </Button>
        </div>
      </div>

      <div className="counter-grid" aria-label="counters">
        {counters
          ? counterCards.map((card) => (
              <div key={card.testId} className={`counter-card counter-card-${card.tone}`}>
                <span className="counter-label">
                  <span className={`dot dot-${card.tone}`} aria-hidden="true" />
                  {card.label}
                </span>
                <p className="counter-value" data-testid={card.testId}>
                  {card.value.toLocaleString(i18n.language)}
                </p>
              </div>
            ))
          : [1, 2, 3, 4, 5].map((key) => (
              <div key={key} className="counter-card">
                <span className="skeleton" style={{ width: 70, height: 9 }} />
                <span className="skeleton" style={{ width: 42, height: 20, marginTop: 8 }} />
              </div>
            ))}
      </div>

      <div className="list-toolbar">
        <SearchInput
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
        <button
          type="button"
          className={`btn btn-secondary btn-sm filters-toggle${filtersOpen ? ' is-open' : ''}`}
          onClick={() => setFiltersOpen((open) => !open)}
          aria-expanded={filtersOpen}
        >
          <SlidersHorizontal size={14} />
          {t('commonExtra.filters')}
          {activeFilterCount > 0 && <span className="filter-count">{activeFilterCount}</span>}
        </button>
        <span className="list-toolbar-meta">
          <ArrowDownUp size={12} />
          {t('tickets.page', { page, pages: totalPages })}
        </span>
      </div>

      <div className="status-filter-row" role="group" aria-label={t('track.status')}>
        {STATUS_FILTERS.map((value) => (
          <button
            key={value || 'all'}
            type="button"
            onClick={() => {
              setStatus(value);
              setPage(1);
            }}
            className={`filter-chip${status === value ? ' is-active' : ''}`}
            aria-pressed={status === value}
          >
            {value && <span className={`status-dot status-dot--${value}`} aria-hidden="true" />}
            {value ? t(STATUS_META[value].labelKey) : t('status.all')}
          </button>
        ))}
        {activeFilterCount > 0 && (
          <button type="button" className="filter-chip" onClick={resetFilters}>
            {t('commonExtra.clear')}
          </button>
        )}
      </div>

      {filtersOpen && (
        <div className="filter-panel">
          <label className="filter-field">
            <span>{t('tickets.from')}</span>
            <input
              type="date"
              className="input"
              value={dateFrom}
              onChange={(e) => {
                setDateFrom(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <label className="filter-field">
            <span>{t('tickets.to')}</span>
            <input
              type="date"
              className="input"
              value={dateTo}
              onChange={(e) => {
                setDateTo(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <p className="filter-note">
            <Filter size={12} /> {t('dashboardExtra.filterNote')}
          </p>
        </div>
      )}

      <div style={{ marginTop: 18 }}>
        {listQuery.isLoading && <LoadingState />}
        {listQuery.isError && <ErrorState message={t('errors.generic')} onRetry={() => void listQuery.refetch()} />}
        {listQuery.data && (
          <div className="ticket-list-card">
            <div className="ticket-list">
              {listQuery.data.items.map((ticket) => (
                <button
                  key={ticket.id}
                  type="button"
                  onClick={() => navigate(`/tickets/${ticket.id}`)}
                  className="ticket-row"
                >
                  <span style={{ minWidth: 0 }}>
                    <span className="ticket-row-min">
                      <span className="ticket-no" dir="ltr">{ticket.ticket_no}</span>
                      <span className="dot" aria-hidden="true" />
                      <span>{companyName(ticket.company_id)}</span>
                      <span className="dot" aria-hidden="true" />
                      <span>{t(`types.${ticket.insurance_type}`)}</span>
                    </span>
                    <span className="ticket-subject" style={{ display: 'block' }}>{ticket.subject}</span>
                    <span className="ticket-company">
                      {t('ticketsExtra.lastUpdate')} · {formatDate(ticket.updated_at, i18n.language)}
                    </span>
                  </span>
                  <span className="ticket-row-end">
                    <span className="ticket-date">
                      <strong>{formatDate(ticket.created_at, i18n.language)}</strong>
                      <span>{t('tickets.lastUpdate')}</span>
                    </span>
                    <StatusChip status={ticket.status} />
                    <span className="row-chevron" aria-hidden="true">
                      <Arrow size={14} />
                    </span>
                  </span>
                </button>
              ))}
            </div>
            {listQuery.data.items.length === 0 && (
              <EmptyState
                title={t('tickets.empty')}
                detail={t('tickets.emptyHint')}
                icon={<SearchX size={22} />}
                action={
                  activeFilterCount > 0 ? (
                    <Button size="sm" variant="secondary" onClick={resetFilters}>
                      {t('commonExtra.clearFilters')}
                    </Button>
                  ) : (
                    <Button size="sm" onClick={() => navigate('/complaints/new')}>
                      {t('tickets.newComplaint')}
                    </Button>
                  )
                }
              />
            )}
            {listQuery.data.items.length > 0 && (
              <div className="ticket-list-foot">
                <span>{t('tickets.showing', { from: (page - 1) * PAGE_SIZE + 1, to: Math.min(page * PAGE_SIZE, listQuery.data.total), total: listQuery.data.total })}</span>
                {totalPages > 1 && (
                  <div className="pager">
                    <button type="button" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} aria-label={t('tickets.prev')}>
                      <Prev size={14} />
                    </button>
                    <span>{page} / {totalPages}</span>
                    <button type="button" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)} aria-label={t('tickets.nextPage')}>
                      <Next size={14} />
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
