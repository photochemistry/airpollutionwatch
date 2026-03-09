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

/** bbox 内で等間隔のグリッド点を生成（経緯度 step、最大点数で制限） */
function buildGridPoints(bbox: BBox, stepDeg = 0.25, maxPerAxis = 12): { lats: number[]; lons: number[] } {
  const lats: number[] = [];
  const lons: number[] = [];
  const latStep = Math.max(stepDeg, (bbox.maxLat - bbox.minLat) / maxPerAxis);
  const lonStep = Math.max(stepDeg, (bbox.maxLon - bbox.minLon) / maxPerAxis);
  for (let lat = bbox.minLat; lat <= bbox.maxLat + 1e-6; lat += latStep) {
    for (let lon = bbox.minLon; lon <= bbox.maxLon + 1e-6; lon += lonStep) {
      lats.push(lat);
      lons.push(lon);
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
    if (!res.ok) return null;
    const data = await res.json() as OpenMeteoLocationResponse[];
    const list = Array.isArray(data) ? data : [data];
    const points: WindPoint[] = [];
    for (const loc of list) {
      const idx = findHourIndex(loc.hourly.time, datetimeIso);
      const speed = loc.hourly.wind_speed_10m?.[idx];
      const dir = loc.hourly.wind_direction_10m?.[idx];
      if (speed != null && Number.isFinite(speed) && dir != null && Number.isFinite(dir)) {
        points.push({
          lat: loc.latitude,
          lon: loc.longitude,
          speed_kmh: speed,
          direction_deg: dir,
        });
      }
    }
    return points;
  } catch {
    return null;
  }
}
