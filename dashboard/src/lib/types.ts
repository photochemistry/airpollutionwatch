import type { OxLevel } from './constants';

export interface LatestRow {
  station_id: string;
  name: string;
  municipality: string;
  lat: number | null;
  lon: number | null;
  ox: number | null;
  nox: number | null;
  no2: number | null;
  pm25: number | null;
  temp: number | null;
  hum: number | null;
  wd: number | null;
  ws: number | null;
  level: OxLevel;
}

export interface OxSeriesItem {
  station_id: string;
  name: string;
  values: { datetime: string; value: number | null }[];
}

export interface BBox {
  minLon: number;
  minLat: number;
  maxLon: number;
  maxLat: number;
}
