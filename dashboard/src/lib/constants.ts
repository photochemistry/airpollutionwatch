/**
 * 光化学オキシダント 注意報・警報の基準値（環境省）
 * API の OX は ppb で返る想定（0.12 ppm = 120 ppb）
 */

export const OX_THRESHOLDS = {
  /** 要注目: 注意報前の警戒目安（100 ppb で色変更） */
  CAUTION_PPB: 100,
  /** 注意報: 1時間値 0.12 ppm = 120 ppb */
  WARNING_PPB: 120,
  /** 警報: 0.24 ppm = 240 ppb */
  ALERT_PPB: 240,
  /** 重大警報: 0.40 ppm = 400 ppb */
  SEVERE_PPB: 400,
} as const;

export type OxLevel = 'normal' | 'forecast' | 'warning' | 'alert' | 'severe';

export function getOxLevel(oxPpb: number | null | undefined): OxLevel {
  if (oxPpb == null || Number.isNaN(oxPpb)) return 'normal';
  if (oxPpb >= OX_THRESHOLDS.SEVERE_PPB) return 'severe';
  if (oxPpb >= OX_THRESHOLDS.ALERT_PPB) return 'alert';
  if (oxPpb >= OX_THRESHOLDS.WARNING_PPB) return 'warning';
  if (oxPpb >= OX_THRESHOLDS.CAUTION_PPB) return 'forecast'; // 100 ppb で要注目（色変更）
  return 'normal';
}

export const OX_LEVEL_LABELS: Record<OxLevel, string> = {
  normal: '平常',
  forecast: '要注目',
  warning: '注意報',
  alert: '警報',
  severe: '重大警報',
};
