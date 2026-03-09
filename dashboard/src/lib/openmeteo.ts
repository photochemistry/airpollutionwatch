/**
 * Open-Meteo Forecast API から風向・風速を取得（クライアント直接取得、APIキー不要）
 * グリッドは OpenMETEO の解像度に合わせず、bbox 内の等間隔点でリクエストする。
 */

import type { BBox } from './types';

const OPENMETEO_BASE = 'https://api.open-meteo.com/v1/forecast';

/** 1地点あたりの Open-Meteo レスポンス（複数地点時は配列で返る） */
interface OpenMeteoLocationResponse {
  latitude: number;
  longitude: number;
  hourly: {
    time: string[];
    wind_speed_10m: number[];
    wind_direction_10m: number[];
  };
}

export interface WindPoint {
  lat: number;
  lon: number;
  speed_kmh: number;
  direction_deg: number;
}

/** 1リクエストあたりの最大地点数（Open-Meteo の制限を考慮） */
const MAX_WIND_POINTS = 100;

/** bbox 内で等間隔のグリッド点を生成。約12〜15km間隔、最大 MAX_WIND_POINTS 点 */
function buildGridPoints(bbox: BBox): { lats: number[]; lons: number[] } {
  const stepDeg = 0.12;
  const latSpan = bbox.maxLat - bbox.minLat;
  const lonSpan = bbox.maxLon - bbox.minLon;
  let nLat = Math.max(1, Math.ceil(latSpan / stepDeg) + 1);
  let nLon = Math.max(1, Math.ceil(lonSpan / stepDeg) + 1);
  if (nLat * nLon > MAX_WIND_POINTS) {
    const r = Math.sqrt(MAX_WIND_POINTS / (nLat * nLon));
    nLat = Math.max(1, Math.floor(nLat * r));
    nLon = Math.max(1, Math.floor(nLon * r));
  }
  const latStep = nLat > 1 ? latSpan / (nLat - 1) : 0;
  const lonStep = nLon > 1 ? lonSpan / (nLon - 1) : 0;
  const lats: number[] = [];
  const lons: number[] = [];
  for (let i = 0; i < nLat; i++) {
    for (let j = 0; j < nLon; j++) {
      lats.push(bbox.minLat + i * latStep);
      lons.push(bbox.minLon + j * lonStep);
    }
  }
  return { lats, lons };
}

/** ISO8601 の時刻文字列を「YYYY-MM-DDTHH:00」に正規化して最も近い時間インデックスを返す */
function findHourIndex(times: string[], datetimeIso: string | null): number {
  if (!datetimeIso || !times.length) return 0;
  const want = datetimeIso.slice(0, 13); // YYYY-MM-DDTHH
  const exact = times.findIndex((t) => t.startsWith(want));
  if (exact >= 0) return exact;
  const wantDate = new Date(datetimeIso).getTime();
  let best = 0;
  let bestDiff = Infinity;
  times.forEach((t, i) => {
    const d = Math.abs(new Date(t).getTime() - wantDate);
    if (d < bestDiff) {
      bestDiff = d;
      best = i;
    }
  });
  return best;
}

/**
 * 指定 bbox と時刻の風向・風速を取得する。
 * プロット範囲内のグリッド点のみ。失敗時は null。
 */
export async function fetchWindForBbox(
  bbox: BBox,
  datetimeIso: string | null
): Promise<WindPoint[] | null> {
  const { lats, lons } = buildGridPoints(bbox);
  if (lats.length === 0) return null;
  const params = new URLSearchParams({
    latitude: lats.join(','),
    longitude: lons.join(','),
    hourly: 'wind_speed_10m,wind_direction_10m',
    timezone: 'Asia/Tokyo',
    past_days: '2',
  });
  const url = `${OPENMETEO_BASE}?${params.toString()}`;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      if (res.status === 429) {
        console.warn('[Open-Meteo] 429 Too Many Requests: Open-Meteo のアクセス制限（レート制限）に引っかかっている可能性があります。');
      } else {
        console.warn('[Open-Meteo] 風データ取得失敗:', res.status, res.statusText);
      }
      return null;
    }
    const data = await res.json() as OpenMeteoLocationResponse[];
    const list = Array.isArray(data) ? data : [data];
    const points: WindPoint[] = [];
    for (const loc of list) {
      const idx = findHourIndex(loc.hourly.time, datetimeIso);
      const speed = loc.hourly.wind_speed_10m?.[idx];
      const dir = loc.hourly.wind_direction_10m?.[idx];
      if (speed != null && Number.isFinite(speed) && dir != null && Number.isFinite(dir)) {
        const lat = loc.latitude;
        const lon = loc.longitude;
        const tol = 0.02;
        if (
          lon >= bbox.minLon - tol && lon <= bbox.maxLon + tol &&
          lat >= bbox.minLat - tol && lat <= bbox.maxLat + tol
        ) {
          points.push({ lat, lon, speed_kmh: speed, direction_deg: dir });
        }
      }
    }
    if (points.length === 0) {
      console.warn('[Open-Meteo] 風データ: bbox 内に有効な点がありませんでした。');
    }
    return points;
  } catch (e) {
    console.warn('[Open-Meteo] 風データ取得エラー:', e);
    return null;
  }
}
