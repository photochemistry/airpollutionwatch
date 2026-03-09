/**
 * airpollutionwatch API クライアント
 * - 開発時: 同じオリジンへ /api でリクエストし、Vite がプロキシで API に転送（CORS 回避）
 * - 本番: VITE_API_BASE_URL が空なら同一オリジン（FastAPI 相乗り）、指定時はその URL にリクエスト
 */

const BASE =
  import.meta.env.DEV
    ? '/api'
    : (typeof import.meta.env.VITE_API_BASE_URL === 'string' && import.meta.env.VITE_API_BASE_URL
        ? import.meta.env.VITE_API_BASE_URL.replace(/\/$/, '')
        : '');

/** 本番で BASE が http なのにページが https だと Mixed Content でブロックされるため、同一ホストならページのオリジンを使う */
function resolveBaseUrl(): string {
  if (import.meta.env.DEV && typeof location !== 'undefined') return location.origin;
  const base = BASE || (typeof location !== 'undefined' ? location.origin : '');
  if (typeof location !== 'undefined' && base && location.origin) {
    try {
      const baseUrl = new URL(base);
      const pageOrigin = new URL(location.origin);
      if (baseUrl.host === pageOrigin.host && baseUrl.protocol !== pageOrigin.protocol)
        return location.origin;
    } catch {
      /* ignore */
    }
  }
  return base;
}

/** 指定 path へのリクエスト URL（デバッグ用） */
export function getApiUrl(path: string, params?: Record<string, string>): string {
  const baseUrl = resolveBaseUrl();
  const pathStr = import.meta.env.DEV ? '/api' + path : path;
  const url = new URL(pathStr, baseUrl || 'http://localhost');
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  return url.toString();
}

export interface PrefectureInfo {
  id: string;
  name_ja: string;
  has_data: boolean;
  region: string;
}

export interface LatestStationValues {
  station_id: string;
  values: Record<string, number | null>;
}

export interface LatestResponse {
  datetime: string;
  stations: LatestStationValues[];
}

export interface StationItem {
  station_id: string;
  pref: string;
  name: string | null;
  name_short: string | null;
  municipality: string | null;
  lat: number | null;
  lon: number | null;
  has_pm25: boolean;
  has_ox: boolean;
}

export interface TimeSeriesPoint {
  datetime: string;
  value: number | null;
}

export interface TimeSeriesSeries {
  station_id: string;
  pollutant: string;
  values: TimeSeriesPoint[];
}

export interface TimeSeriesResponse {
  timeseries: TimeSeriesSeries[];
}

async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const baseUrl = resolveBaseUrl();
  const pathStr = import.meta.env.DEV ? '/api' + path : path;
  const url = new URL(pathStr, baseUrl || 'http://localhost');
  if (params) {
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  }
  const res = await fetch(url.toString());
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json() as Promise<T>;
}

/** 都道府県一覧（id, name_ja, has_data, region） */
export async function fetchPrefectures(): Promise<PrefectureInfo[]> {
  return get<PrefectureInfo[]>('/v1/prefectures');
}

/** 都道府県の最新値（局ごと） */
export async function fetchLatest(
  pref: string,
  pollutants: string = 'ox,nox,no2,pm25,temp,hum,wd,ws'
): Promise<LatestResponse> {
  return get<LatestResponse>('/v1/latest', { pref, pollutants });
}

/** 測定局一覧（県・測定項目で絞り込み） */
export async function fetchStations(
  pref: string,
  has?: string
): Promise<StationItem[]> {
  const params: Record<string, string> = { pref };
  if (has) params.has = has;
  return get<StationItem[]>('/v1/stations', params);
}

/** 時系列データ（局 or 県、期間、format=series） */
export async function fetchMeasurementsSeries(
  pref: string,
  fromIso: string,
  toIso: string,
  pollutants: string = 'ox,nox,no2'
): Promise<TimeSeriesResponse> {
  return get<TimeSeriesResponse>('/v1/measurements', {
    pref,
    from: fromIso,
    to: toIso,
    pollutants,
    format: 'series',
  });
}


/** 指定都道府県の輪郭（簡略化済み rings）と輪郭から算出した bbox。県単位で取得するため軽量。 */
export interface PrefectureOutlineResponse {
  rings: [number, number][][];
  /** [minLon, minLat, maxLon, maxLat]。輪郭から事前計算済み。 */
  bbox?: [number, number, number, number];
}
export async function fetchPrefectureOutline(prefId: string): Promise<PrefectureOutlineResponse> {
  return get<PrefectureOutlineResponse>(`/v1/geojson/outline/${prefId}`);
}

/** グリッド field（bbox 内の補間値・地図オーバーレイ用） */
export interface GridFieldResponse {
  z: number;
  datetime: string;
  method: string;
  pollutant: string;
  tile_x_min: number;
  tile_x_max: number;
  tile_y_min: number;
  tile_y_max: number;
  values: (number | null)[][];
}

export async function fetchGridField(
  bbox: string,
  pollutant: string,
  datetimeIso: string,
  z: number = 12,
  method: string = 'atps',
  smoothing: string = '0.007'
): Promise<GridFieldResponse> {
  return get<GridFieldResponse>('/v1/grid/field', {
    bbox,
    pollutant,
    datetime: datetimeIso,
    z: String(z),
    method,
    smoothing,
  });
}

export { BASE as apiBaseUrl };
