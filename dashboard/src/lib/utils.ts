import type { OxLevel } from './constants';

export function normalizeStationId(id: string): string {
  const n = id.replace(/\D/g, '');
  return n ? n.padStart(8, '0').slice(-8) : id;
}

export function levelClass(level: OxLevel): string {
  return 'level-' + level;
}

export function formatNum(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—';
  return String(Math.round(v * 10) / 10);
}

export function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit', hour12: false });
  } catch {
    return iso;
  }
}
