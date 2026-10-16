import { useState } from 'react';
import type { DateRange } from '../types';
import { displayRange, presetRange } from '../utils/date';
import { Icon } from './Icon';

interface DateRangePickerProps {
  value: DateRange;
  onChange: (range: DateRange) => void;
}

const presets: Array<{ value: Exclude<DateRange['preset'], 'custom'>; label: string }> = [
  { value: '7d', label: 'آخر ٧ أيام' },
  { value: '30d', label: 'آخر ٣٠ يوماً' },
  { value: '90d', label: 'آخر ٩٠ يوماً' },
  { value: 'year', label: 'منذ بداية العام' },
];

export function DateRangePicker({ value, onChange }: DateRangePickerProps) {
  const [open, setOpen] = useState(false);
  const applyCustom = (key: 'from' | 'to', next: string) => {
    const candidate = { ...value, [key]: next, preset: 'custom' as const };
    if (candidate.from && candidate.to && candidate.from <= candidate.to) onChange(candidate);
  };

  return (
    <div className="date-picker-wrap">
      <button
        className={`date-picker-trigger${open ? ' is-open' : ''}`}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="date-picker-icon"><Icon name="calendar" size={17} /></span>
        <span className="date-picker-copy">
          <small>الفترة الزمنية</small>
          <strong>{displayRange(value)}</strong>
        </span>
        <Icon name="chevron" className="date-picker-chevron" size={15} />
      </button>
      {open && (
        <div className="date-picker-menu" role="dialog" aria-label="اختيار الفترة الزمنية">
          <p className="menu-label">فترة جاهزة</p>
          <div className="preset-grid">
            {presets.map((preset) => (
              <button
                className={`preset-option${value.preset === preset.value ? ' selected' : ''}`}
                key={preset.value}
                type="button"
                onClick={() => {
                  onChange(presetRange(preset.value));
                  setOpen(false);
                }}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <div className="custom-date-divider"><span />أو حدّد نطاقاً مخصصاً<span /></div>
          <div className="custom-date-fields">
            <label>
              <span>من</span>
              <input
                aria-label="من تاريخ"
                type="date"
                value={value.from}
                max={value.to || undefined}
                onChange={(event) => applyCustom('from', event.target.value)}
              />
            </label>
            <label>
              <span>إلى</span>
              <input
                aria-label="إلى تاريخ"
                type="date"
                value={value.to}
                min={value.from || undefined}
                max={new Date().toISOString().slice(0, 10)}
                onChange={(event) => applyCustom('to', event.target.value)}
              />
            </label>
          </div>
          <button className="button button-dark date-apply" type="button" onClick={() => setOpen(false)}>
            تطبيق الفترة
          </button>
        </div>
      )}
    </div>
  );
}
