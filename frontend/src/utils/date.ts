import type { DateRange } from '../types';

export function toLocalDateString(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

export function presetRange(preset: Exclude<DateRange['preset'], 'custom'>, now = new Date()): DateRange {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  if (preset === '7d') start.setDate(start.getDate() - 6);
  if (preset === '30d') start.setDate(start.getDate() - 29);
  if (preset === '90d') start.setDate(start.getDate() - 89);
  if (preset === 'year') start.setMonth(0, 1);
  return { from: toLocalDateString(start), to: toLocalDateString(end), preset };
}

export function rangeBounds(range: DateRange): { date_from: string; date_to: string } {
  const start = new Date(`${range.from}T00:00:00`);
  const end = new Date(`${range.to}T23:59:59.999`);
  return { date_from: start.toISOString(), date_to: end.toISOString() };
}

export function displayRange(range: DateRange): string {
  const format = (value: string) =>
    new Intl.DateTimeFormat('ar-EG', { day: 'numeric', month: 'short' }).format(new Date(`${value}T12:00:00`));
  return `${format(range.from)} — ${format(range.to)}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ar-EG', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function formatNumber(value: number | null | undefined, maximumFractionDigits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('ar-EG', { maximumFractionDigits }).format(value);
}
