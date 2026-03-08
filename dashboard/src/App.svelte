<script lang="ts">
  import { onMount, tick } from 'svelte';
  import Plotly from 'plotly.js-dist-min';
  import {
    fetchLatest,
    fetchStations,
    fetchMeasurementsSeries,
    fetchGridField,
    type LatestResponse,
    type LatestStationValues,
    type StationItem,
    type TimeSeriesResponse,
  } from './lib/api';
  import { getOxLevel, OX_THRESHOLDS, OX_LEVEL_LABELS, type OxLevel } from './lib/constants';
  import type { GridFieldResponse } from './lib/api';

  const PREF = 'kanagawa';

  /** 表示用: OX 値をこの倍率で表示（1 で実値）。 */
  const OX_DISPLAY_MULTIPLIER: number = 1;

  let latest: LatestResponse | null = null;
  let stations: StationItem[] = [];
  let timeseries: TimeSeriesResponse | null = null;
  let loading = true;
  let error: string | null = null;
  let lastFetched: Date | null = null;
  let plotDiv: HTMLDivElement | null = null;
  let mapPlotDiv: HTMLDivElement | null = null;
  let debugPlot3Div: HTMLDivElement | null = null;
  let gridFieldData: GridFieldResponse | null = null;
  /** 神奈川県輪郭 [lon, lat][]（GeoJSON から取得、取得失敗時は bbox 矩形） */
  let kanagawaOutline: [number, number][] = [];
  /** 地図グラデーション用 Canvas の data URL（Plotly で表示されない場合の img 用） */
  let mapGradientDataUrl: string | null = null;
  const KANAGAWA_BBOX = '138.9,35.1,139.85,35.7';
  const KANAGAWA_BBOX_NUM = { minLon: 138.9, minLat: 35.1, maxLon: 139.85, maxLat: 35.7 };
  /** true のとき地図の代わりに sin(x)*cos(y) のテストプロットを表示（問題切り分け用） */
  const DEBUG_PLOT = false;
  /** true のとき xy は神奈川範囲・z は sin(x)*cos(y) で heatmap を表示（ログ・グラフの切り分け用） */
  const DEBUG_HEATMAP_SINCOS = false;

  function normalizeStationId(id: string): string {
    const n = id.replace(/\D/g, '');
    return n ? n.padStart(8, '0').slice(-8) : id;
  }

  const stationMap = new Map<string, StationItem>();
  $: {
    stationMap.clear();
    stations.forEach((s) => {
      const key = normalizeStationId(s.station_id);
      stationMap.set(key, s);
    });
  }

  let latestWithNames: Array<{
    station_id: string; name: string; municipality: string;
    ox: number | null; nox: number | null; no2: number | null; pm25: number | null;
    temp: number | null; hum: number | null; wd: number | null; ws: number | null;
    level: OxLevel;
  }> = [];
  $: latestWithNames = latest
    ? (() => {
        const byNorm = new Map<string, LatestStationValues>();
        for (const s of latest!.stations) {
          const key = normalizeStationId(s.station_id);
          const existing = byNorm.get(key);
          const hasOx = (v: LatestStationValues) => v.values['OX'] != null && !Number.isNaN(v.values['OX']);
          if (!existing || (hasOx(s) && !hasOx(existing))) byNorm.set(key, s);
        }
        return Array.from(byNorm.values())
          .map((s) => {
            const key = normalizeStationId(s.station_id);
            const meta = stationMap.get(key);
            const rawOx = s.values['OX'] ?? null;
            const ox = rawOx != null ? rawOx * OX_DISPLAY_MULTIPLIER : null;
            const level = getOxLevel(ox);
            const name = meta?.name ?? meta?.name_short ?? null;
            return {
              station_id: s.station_id,
              name: name ?? s.station_id,
              municipality: meta?.municipality ?? '—',
              ox, nox: s.values['NOX'] ?? null, no2: s.values['NO2'] ?? null,
              pm25: s.values['PM25'] ?? null, temp: s.values['TEMP'] ?? null,
              hum: s.values['HUM'] ?? null, wd: s.values['WD'] ?? null, ws: s.values['WS'] ?? null,
              level,
            };
          })
          .filter((row) => row.name !== row.station_id)
          .sort((a, b) => (b.ox ?? -1) - (a.ox ?? -1));
      })()
    : [];

  let oxSeriesByStation: Array<{ station_id: string; name: string; values: { datetime: string; value: number | null }[] }> = [];
  $: oxSeriesByStation = timeseries?.timeseries
    ? (() => {
        const byStation = new Map<string, { datetime: string; value: number | null }[]>();
        for (const ts of timeseries.timeseries) {
          if (ts.pollutant !== 'OX') continue;
          const key = normalizeStationId(ts.station_id);
          if (!byStation.has(key)) byStation.set(key, ts.values);
        }
        return Array.from(byStation.entries()).map(([key, values]) => ({
          station_id: key,
          name: stationMap.get(key)?.name_short ?? stationMap.get(key)?.name ?? key,
          values,
        }));
      })()
    : [];

  async function load() {
    loading = true;
    error = null;
    try {
      const [latestRes, stationsRes, tsRes] = await Promise.all([
        fetchLatest(PREF, 'ox,nox,no2,pm25,temp,hum,wd,ws'),
        fetchStations(PREF, 'ox'),
        loadLast24hSeries(),
      ]);
      latest = latestRes;
      stations = stationsRes;
      timeseries = tsRes;
      lastFetched = new Date();
      if (latestRes.datetime) {
        try {
          gridFieldData = await fetchGridField(KANAGAWA_BBOX, 'ox', latestRes.datetime, 13, 'atps', '0.007');
          const v = gridFieldData?.values;
          console.log('[load] gridField 取得成功 values.length=', v?.length ?? 0, 'values[0]?.length=', Array.isArray(v?.[0]) ? (v[0] as unknown[]).length : '-');
        } catch (e) {
          gridFieldData = null;
          console.warn('[load] gridField 取得失敗', e);
        }
      } else {
        gridFieldData = null;
        console.log('[load] latestRes.datetime がないため gridField は取得しません');
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
      // データ取得後に地図を再描画（リアクティブが先に走って null で描画されることがあるため）
      await tick();
      if (mapPlotDiv) drawMapPlotly();
    }
  }

  function toJstIsoHour(d: Date): string {
    const jst = new Date(d.getTime() + 9 * 60 * 60 * 1000);
    const y = jst.getUTCFullYear();
    const m = jst.getUTCMonth() + 1;
    const day = jst.getUTCDate();
    const h = jst.getUTCHours();
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${y}-${pad(m)}-${pad(day)}T${pad(h)}:00:00+09:00`;
  }

  async function loadLast24hSeries(): Promise<TimeSeriesResponse | null> {
    const toDate = new Date();
    const fromDate = new Date(toDate.getTime() - 24 * 60 * 60 * 1000);
    const fromIso = toJstIsoHour(fromDate);
    const toIso = toJstIsoHour(toDate);
    try {
      return await fetchMeasurementsSeries(PREF, fromIso, toIso, 'ox');
    } catch {
      return null;
    }
  }

  function levelClass(level: OxLevel): string {
    return 'level-' + level;
  }

  function formatNum(v: number | null | undefined): string {
    if (v == null || Number.isNaN(v)) return '—';
    return String(Math.round(v * 10) / 10);
  }

  function formatTime(iso: string): string {
    try {
      return new Date(iso).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit', hour12: false });
    } catch {
      return iso;
    }
  }

  const OX_REFERENCE_PPB = 120;

  function drawPlotly() {
    if (!plotDiv || oxSeriesByStation.length === 0) return;
    const traces = oxSeriesByStation.map((series) => ({
      x: series.values.map((p) => p.datetime),
      y: series.values.map((p) => (p.value != null ? p.value * OX_DISPLAY_MULTIPLIER : null)),
      name: series.name,
      type: 'scatter' as const,
      mode: 'lines' as const,
      line: { width: 1.5 },
      connectgaps: false,
    }));
    const layout = {
      margin: { t: 24, r: 24, b: 40, l: 52 },
      xaxis: {
        type: 'date',
        title: { text: '時刻（過去24時間）' },
        rangeslider: { visible: false },
      },
      yaxis: {
        title: { text: 'OX (ppb)' },
        rangemode: 'tozero' as const,
      },
      shapes: [
        {
          type: 'line' as const,
          xref: 'paper',
          yref: 'y',
          x0: 0,
          x1: 1,
          y0: OX_REFERENCE_PPB,
          y1: OX_REFERENCE_PPB,
          line: { color: '#e65100', width: 1.5, dash: 'dash' },
        },
      ],
      annotations: [
        {
          xref: 'paper',
          yref: 'y',
          x: 1,
          y: OX_REFERENCE_PPB,
          xanchor: 'left',
          text: ` ${OX_REFERENCE_PPB} ppb`,
          showarrow: false,
          font: { size: 11, color: '#e65100' },
        },
      ],
      showlegend: true,
      legend: { x: 1, y: 1, xanchor: 'left' },
      height: 420,
    };
    Plotly.react(plotDiv, traces, layout, { responsive: true, displayModeBar: true });
    requestAnimationFrame(() => {
      if (plotDiv && typeof Plotly.Plots?.resize === 'function') Plotly.Plots.resize(plotDiv);
    });
  }

  $: if (plotDiv && oxSeriesByStation.length > 0) {
    drawPlotly();
  }

  /** タイル座標 (x,y) @ zoom を経緯度 [lon, lat] に変換 */
  function tileXYToLonLat(x: number, y: number, zoom: number): [number, number] {
    const n = 2 ** zoom;
    const lon = (x / n) * 360 - 180;
    const latRad = Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n)));
    const lat = (180 / Math.PI) * latRad;
    return [lon, lat];
  }

  const REF_PPB = 120;

  function valueToRgbaRelative(v: number | null, vMin: number, vMax: number): string {
    if (v == null || Number.isNaN(v)) return 'rgba(200,200,200,0.4)';
    const range = vMax - vMin;
    const t = range > 0 ? Math.max(0, Math.min(1, (v - vMin) / range)) : 0;
    const a = 0.85;
    let r: number, g: number, b: number;
    if (t <= 0.25) {
      const s = t / 0.25;
      r = 34 + s * (76 - 34);
      g = 139 + s * (175 - 139);
      b = 34 + s * (74 - 34);
    } else if (t <= 0.5) {
      const s = (t - 0.25) / 0.25;
      r = 76 + s * (255 - 76);
      g = 175 + s * (235 - 175);
      b = 74 + s * (59 - 74);
    } else if (t <= 0.75) {
      const s = (t - 0.5) / 0.25;
      r = 255;
      g = 235 + s * (224 - 235);
      b = 59 + s * (170 - 59);
    } else {
      const s = (t - 0.75) / 0.25;
      r = 255;
      g = 224 - s * 224;
      b = 170 - s * 170;
    }
    return `rgba(${Math.round(r)},${Math.round(g)},${Math.round(b)},${a})`;
  }

  /** グリッドを Canvas で描画し、Plotly image 用の data URL と範囲を返す。 */
  function gridToImageTrace(
    data: GridFieldResponse,
    displayMultiplier: number
  ): { source: string; x0: number; y0: number; dx: number; dy: number } | null {
    const { values, tile_x_min, tile_x_max, tile_y_min, tile_y_max, z: zoom } = data;
    const nx = tile_x_max - tile_x_min + 1;
    const ny = tile_y_max - tile_y_min + 1;
    if (nx < 1 || ny < 1 || !values?.length || !values[0]?.length) return null;
    const getVal = (row: number, col: number): number | null => {
      const v = values[row]?.[col];
      if (v == null || Number.isNaN(v)) return null;
      return v * displayMultiplier;
    };
    let vMin = Infinity;
    let vMax = -Infinity;
    for (let row = 0; row < ny; row++) {
      for (let col = 0; col < nx; col++) {
        const val = getVal(row, col);
        if (val != null) {
          vMin = Math.min(vMin, val);
          vMax = Math.max(vMax, val);
        }
      }
    }
    if (vMin > vMax) {
      vMin = 0;
      vMax = REF_PPB;
    } else if (vMin === vMax) {
      vMin = Math.max(0, vMin - 5);
      vMax = vMax + 5;
    }
    const [westLon, southLat] = tileXYToLonLat(tile_x_min + 0.5, tile_y_max + 0.5, zoom);
    const [eastLon, northLat] = tileXYToLonLat(tile_x_max + 0.5, tile_y_min + 0.5, zoom);
    const canvas = document.createElement('canvas');
    canvas.width = nx;
    canvas.height = ny;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;
    for (let row = 0; row < ny; row++) {
      for (let col = 0; col < nx; col++) {
        const val = getVal(row, col);
        ctx.fillStyle = valueToRgbaRelative(val, vMin, vMax);
        ctx.fillRect(col, row, 1, 1);
      }
    }
    const dx = (eastLon - westLon) / nx;
    const dy = (northLat - southLat) / ny;
    return {
      source: canvas.toDataURL('image/png'),
      x0: westLon + dx / 2,
      y0: southLat + dy / 2,
      dx,
      dy,
    };
  }

  async function loadKanagawaOutline(): Promise<void> {
    try {
      const base = (import.meta as { env?: { BASE_URL?: string } }).env?.BASE_URL ?? '/';
      const r = await fetch(`${base}kanagawa-outline.json`);
      if (!r.ok) throw new Error(String(r.status));
      const geojson = await r.json();
      const coords = geojson?.geometry?.coordinates?.[0];
      if (Array.isArray(coords) && coords.length > 0) {
        kanagawaOutline = coords.map((c: number[]) => [c[0], c[1]] as [number, number]);
        return;
      }
    } catch {
      // ignore
    }
    kanagawaOutline = [
      [KANAGAWA_BBOX_NUM.minLon, KANAGAWA_BBOX_NUM.minLat],
      [KANAGAWA_BBOX_NUM.maxLon, KANAGAWA_BBOX_NUM.minLat],
      [KANAGAWA_BBOX_NUM.maxLon, KANAGAWA_BBOX_NUM.maxLat],
      [KANAGAWA_BBOX_NUM.minLon, KANAGAWA_BBOX_NUM.maxLat],
      [KANAGAWA_BBOX_NUM.minLon, KANAGAWA_BBOX_NUM.minLat],
    ];
  }

  /** グリッド API 応答を Plotly heatmap 用の x, y, z に変換。API は values[0]=南。values が 1次元の場合は row-major で 2次元に組み直す。 */
  function gridToHeatmapData(
    data: GridFieldResponse,
    displayMultiplier: number
  ): { x: number[]; y: number[]; z: number[][] } | null {
    const { tile_x_min, tile_x_max, tile_y_min, tile_y_max, z: zoom } = data;
    let values = data.values;
    const valuesLen = values == null ? 0 : (Array.isArray(values) ? values.length : (values as unknown as ArrayLike<unknown>).length);
    const firstRowLen = values != null && values[0] != null && Array.isArray(values[0]) ? (values[0] as unknown[]).length : null;
    console.log('[gridToHeatmapData] 呼び出し: valuesLen=', valuesLen, 'firstRowLen=', firstRowLen, 'tile_x=', tile_x_min, '..', tile_x_max, 'tile_y=', tile_y_min, '..', tile_y_max);
    if (values == null) {
      console.warn('[gridToHeatmapData] values が null のため null を返します');
      return null;
    }
    if (!Array.isArray(values)) values = Array.from(values as unknown as ArrayLike<unknown>);
    const nx = tile_x_max - tile_x_min + 1;
    const ny = tile_y_max - tile_y_min + 1;
    if (nx < 1 || ny < 1) {
      console.warn('[gridToHeatmapData] nx または ny が 0 以下のため null を返します nx=', nx, 'ny=', ny);
      return null;
    }

    const expectedLen = nx * ny;
    const firstRow = values[0];
    const is2d = Array.isArray(firstRow);
    let rows: (number | null)[][];

    if (is2d && values.length === ny) {
      rows = (values as (number | null)[][]).map((rawRow) => {
        const rowArr = Array.isArray(rawRow) ? rawRow : [];
        return Array.from({ length: nx }, (_, col) => {
          const v = rowArr[col];
          const n = Number(v);
          return typeof n === 'number' && !Number.isNaN(n) ? n : null;
        });
      });
    } else if (is2d && values.length > 0) {
      const flat = (values as (number | null)[][]).flat();
      if (flat.length === expectedLen) {
        rows = [];
        for (let row = 0; row < ny; row++) {
          const r: (number | null)[] = [];
          for (let col = 0; col < nx; col++) {
            const v = flat[row * nx + col];
            const n = Number(v);
            r.push(typeof n === 'number' && !Number.isNaN(n) ? n : null);
          }
          rows.push(r);
        }
        console.log('[gridToHeatmapData] values 2次元だが行数が ny と異なるため flat で再構成しました');
      } else {
        console.warn('[gridToHeatmapData] values の形状が想定外: length=', values.length, 'is2d=', is2d, 'ny=', ny, 'nx=', nx);
        return null;
      }
    } else if (!is2d && typeof firstRow === 'number' && values.length === expectedLen) {
      const flat = values as (number | null)[];
      rows = [];
      for (let row = 0; row < ny; row++) {
        const r: (number | null)[] = [];
        for (let col = 0; col < nx; col++) {
          const v = flat[row * nx + col];
          const n = Number(v);
          r.push(typeof n === 'number' && !Number.isNaN(n) ? n : null);
        }
        rows.push(r);
      }
      console.log('[gridToHeatmapData] values を 1次元から 2次元に変換しました (row-major, ny*nx=', ny * nx, ')');
    } else if (!is2d && values.length >= expectedLen) {
      const flat = Array.from(values as ArrayLike<unknown>);
      rows = [];
      for (let row = 0; row < ny; row++) {
        const r: (number | null)[] = [];
        for (let col = 0; col < nx; col++) {
          const v = flat[row * nx + col];
          const n = Number(v);
          r.push(typeof n === 'number' && !Number.isNaN(n) ? n : null);
        }
        rows.push(r);
      }
      console.log('[gridToHeatmapData] values を 1次元風から 2次元に変換しました length=', flat.length, 'expected=', expectedLen);
    } else {
      console.warn('[gridToHeatmapData] values の形状が想定外: length=', values.length, 'is2d=', is2d, 'ny=', ny, 'nx=', nx);
      return null;
    }

    const x: number[] = [];
    const y: number[] = [];
    for (let col = 0; col < nx; col++) {
      const [lon] = tileXYToLonLat(tile_x_min + col + 0.5, tile_y_min + 0.5, zoom);
      x.push(lon);
    }
    for (let row = 0; row < ny; row++) {
      const [, lat] = tileXYToLonLat(tile_x_min + 0.5, tile_y_max + 0.5 - row, zoom);
      y.push(lat);
    }
    const z: number[][] = rows.map((row) =>
      row.map((v) => (v != null ? v * displayMultiplier : 0))
    );
    const zFlat = z.flat();
    const sample = zFlat.filter((v) => v > 0);
    const zMin = zFlat.length ? Math.min(...zFlat) : 0;
    const zMax = zFlat.length ? Math.max(...zFlat) : 0;
    console.log(
      '[gridToHeatmapData] nx=', nx, 'ny=', ny,
      'nonZero=', sample.length, 'zMin=', zMin.toFixed(1), 'zMax=', zMax.toFixed(1)
    );
    return { x, y, z };
  }

  /** デバッグ用: sin(x)*cos(y) の 2D 配列を生成（x,y は 0〜2π） */
  function makeSinCosData(): { x: number[]; y: number[]; z: number[][] } {
    const n = 40;
    const x: number[] = [];
    const y: number[] = [];
    for (let i = 0; i < n; i++) {
      x.push((i / (n - 1)) * 2 * Math.PI);
      y.push((i / (n - 1)) * 2 * Math.PI);
    }
    const z: number[][] = [];
    for (let i = 0; i < n; i++) {
      const row: number[] = [];
      for (let j = 0; j < n; j++) {
        row.push(Math.sin(x[j]) * Math.cos(y[i]));
      }
      z.push(row);
    }
    return { x, y, z };
  }

  /** 神奈川県の xy 範囲で z = sin(x)*cos(y) を 0〜250 にスケールした heatmap 用データ */
  function makeKanagawaSinCosData(): { x: number[]; y: number[]; z: number[][] } {
    const nx = 40;
    const ny = 30;
    const { minLon, maxLon, minLat, maxLat } = KANAGAWA_BBOX_NUM;
    const x: number[] = [];
    const y: number[] = [];
    for (let j = 0; j < nx; j++) {
      x.push(minLon + (j / (nx - 1)) * (maxLon - minLon));
    }
    for (let i = 0; i < ny; i++) {
      y.push(minLat + (i / (ny - 1)) * (maxLat - minLat));
    }
    const z: number[][] = [];
    for (let i = 0; i < ny; i++) {
      const row: number[] = [];
      for (let j = 0; j < nx; j++) {
        const v = Math.sin(x[j]) * Math.cos(y[i]); // [-1, 1]
        row.push((v + 1) * 125); // [0, 250] で colorscale に合わせる
      }
      z.push(row);
    }
    return { x, y, z };
  }

  function drawMapPlotly(): void {
    if (!mapPlotDiv) return;

    if (DEBUG_PLOT) {
      const { x, y, z } = makeSinCosData();
      const traces2d = [
        {
          x,
          y,
          z,
          type: 'heatmap',
          colorscale: 'RdBu',
          zmin: -1,
          zmax: 1,
        },
      ];
      const layout2d = {
        title: { text: 'デバッグ: sin(x)*cos(y) (2D heatmap)' },
        xaxis: { title: 'x' },
        yaxis: { title: 'y', scaleanchor: 'x' },
        margin: { t: 40, r: 24, b: 40, l: 52 },
        height: 420,
      };
      Plotly.react(mapPlotDiv, traces2d, layout2d, { responsive: true, displayModeBar: true });
      if (debugPlot3Div) {
        const traces3d = [
          {
            x,
            y,
            z,
            type: 'surface',
            colorscale: 'RdBu',
            zmin: -1,
            zmax: 1,
          },
        ];
        const layout3d = {
          title: { text: 'デバッグ: sin(x)*cos(y) (3D surface)' },
          margin: { t: 40, r: 24, b: 40, l: 52 },
          height: 420,
          scene: {
            xaxis: { title: 'x' },
            yaxis: { title: 'y' },
            zaxis: { title: 'z' },
          },
        };
        Plotly.react(debugPlot3Div, traces3d, layout3d, { responsive: true, displayModeBar: true });
      }
      return;
    }

    const heatmapData = DEBUG_HEATMAP_SINCOS
      ? makeKanagawaSinCosData()
      : (gridFieldData ? gridToHeatmapData(gridFieldData, OX_DISPLAY_MULTIPLIER) : null);
    mapGradientDataUrl = null;
    const traces: Record<string, unknown>[] = [];
    if (heatmapData) {
      const x = heatmapData.x.slice();
      const y = heatmapData.y.slice();
      const z = heatmapData.z.map((row) => row.slice());
      if (DEBUG_HEATMAP_SINCOS) {
        console.log('[drawMapPlotly] DEBUG_HEATMAP_SINCOS: Kanagawa xy + sin(x)*cos(y), x.len=', x.length, 'y.len=', y.length);
      } else {
        console.log('[drawMapPlotly] adding heatmap trace, traces.length will be', traces.length + 1);
      }
      const colorscale: [number, string][] = [
        [0, 'rgb(34,139,34)'],
        [0.25, 'rgb(76,175,74)'],
        [0.5, 'rgb(255,235,59)'],
        [0.75, 'rgb(255,152,0)'],
        [1, 'rgb(227,26,28)'],
      ];
      traces.push({
        x,
        y,
        z,
        type: 'contour',
        zmin: 0,
        zmax: 250,
        colorscale,
        colorbar: { title: 'OX (ppb)' },
        contours: {
          showlabels: true,
          showlines: true,
          coloring: 'heatmap',
        },
      });
    } else {
      if (gridFieldData) {
        console.warn('[drawMapPlotly] no heatmapData ですが gridFieldData はあります。上記 [gridToHeatmapData] のログで原因を確認してください。');
      } else {
        console.log('[drawMapPlotly] no heatmapData (gridFieldData が null＝API 未取得または取得失敗)');
      }
    }
    if (kanagawaOutline.length > 0) {
      const lons = kanagawaOutline.map((c) => c[0]);
      const lats = kanagawaOutline.map((c) => c[1]);
      traces.push({
        x: lons,
        y: lats,
        type: 'scatter',
        mode: 'lines',
        line: { color: '#333', width: 2 },
        fill: 'none',
        showlegend: false,
      });
    }
    const layout = {
      xaxis: {
        title: '経度',
        range: [KANAGAWA_BBOX_NUM.minLon, KANAGAWA_BBOX_NUM.maxLon] as [number, number],
        constrain: 'domain',
      },
      yaxis: {
        title: '緯度',
        range: [KANAGAWA_BBOX_NUM.minLat, KANAGAWA_BBOX_NUM.maxLat] as [number, number],
        scaleanchor: 'x',
        scaleratio: 1,
      },
      margin: { t: 24, r: 24, b: 40, l: 52 },
      height: 420,
      showlegend: false,
    };
    Plotly.react(mapPlotDiv, traces, layout, { responsive: true, displayModeBar: true });
    requestAnimationFrame(() => {
      if (mapPlotDiv && typeof Plotly.Plots?.resize === 'function') Plotly.Plots.resize(mapPlotDiv);
    });
    setTimeout(() => {
      if (mapPlotDiv && typeof Plotly.Plots?.resize === 'function') Plotly.Plots.resize(mapPlotDiv);
    }, 300);
  }

  $: if (mapPlotDiv) {
    void gridFieldData;
    void kanagawaOutline.length;
    void debugPlot3Div;
    drawMapPlotly();
  }

  onMount(() => {
    load();
    loadKanagawaOutline();
  });
</script>

<main class="dashboard">
  <header class="header">
    <h1>神奈川県 光化学オキシダント 監視ダッシュボード</h1>
    <p class="subtitle">大気環境常時監視データ（airpollutionwatch API）</p>
    <div class="header-actions">
      <button type="button" onclick={load} disabled={loading}>{loading ? '取得中…' : '更新'}</button>
      {#if lastFetched}
        <span class="updated">最終更新: {lastFetched.toLocaleString('ja-JP')}</span>
      {/if}
    </div>
  </header>

  {#if error}
    <div class="error" role="alert">
      <strong>データ取得エラー:</strong> {error}
      <br /><small>API ベース URL を確認してください（.env の VITE_API_BASE_URL）</small>
    </div>
  {/if}

  <div class="charts-row">
    <section class="section map-section">
      <h2>神奈川県 OX 分布（補間・等高線）</h2>
      {#if latest?.datetime}
        <p class="map-datetime">対象時刻: {latest.datetime}</p>
      {/if}
      {#if latest && !gridFieldData && !loading}
        <p class="muted">グリッドデータを取得できませんでした（API /v1/grid/field を確認してください）。</p>
      {/if}
      <div class="plotly-map-wrap">
        {#if DEBUG_PLOT}
          <p class="muted">DEBUG_PLOT=true: sin(x)*cos(y) で Plotly の表示を確認しています。</p>
        {:else if DEBUG_HEATMAP_SINCOS}
          <p class="muted">DEBUG_HEATMAP_SINCOS=true: xy は神奈川範囲・z は sin(x)*cos(y) のテスト表示です。</p>
          <div class="plotly-container plotly-map" bind:this={mapPlotDiv}></div>
          <div class="plotly-container plotly-debug3" bind:this={debugPlot3Div}></div>
        {:else}
          {#if mapGradientDataUrl}
            <img class="map-gradient-img" src={mapGradientDataUrl} alt="OX分布" />
          {/if}
          <div class="plotly-container plotly-map" bind:this={mapPlotDiv}></div>
        {/if}
      </div>
      <p class="map-legend">階調: 低（緑）→ 高（赤）。枠線は神奈川県の範囲です。</p>
    </section>
    <section class="section timeseries">
      <h2>過去24時間の OX 推移（1時間値）</h2>
      {#if oxSeriesByStation.length > 0}
        <div class="plotly-container" bind:this={plotDiv}></div>
      {:else}
        <p class="muted">時系列データがありません（過去24時間のデータまたはAPI未取得）</p>
      {/if}
    </section>
  </div>

  <section class="section latest">
    <h2>最新の測定値（OX 高い順）</h2>
    {#if OX_DISPLAY_MULTIPLIER !== 1}
      <p class="simulate-note">※ OX 値はシミュレーション表示（実値×{OX_DISPLAY_MULTIPLIER}）</p>
    {/if}
    {#if latest?.datetime}
      <p class="target-datetime">対象時刻: {latest.datetime}</p>
    {/if}
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>測定局</th><th>市区町村</th><th>OX (ppb)</th><th>レベル</th>
            <th>NOx</th><th>NO2</th><th>PM2.5</th><th>気温</th><th>湿度</th><th>風向</th><th>風速</th>
          </tr>
        </thead>
        <tbody>
          {#each latestWithNames as row}
            <tr class={levelClass(row.level)}>
              <td class="station-name">{row.name}</td>
              <td>{row.municipality}</td>
              <td class="num ox">{formatNum(row.ox)}</td>
              <td><span class="badge {levelClass(row.level)}">{OX_LEVEL_LABELS[row.level]}</span></td>
              <td class="num">{formatNum(row.nox)}</td>
              <td class="num">{formatNum(row.no2)}</td>
              <td class="num">{formatNum(row.pm25)}</td>
              <td class="num">{formatNum(row.temp)}</td>
              <td class="num">{formatNum(row.hum)}</td>
              <td class="num">{row.wd != null ? row.wd + '°' : '—'}</td>
              <td class="num">{formatNum(row.ws)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="table-legend">
      <h3>注意報・警報の基準（1時間値）</h3>
      <ul class="thresholds">
        <li class="level-normal">{OX_LEVEL_LABELS.normal}: &lt; {OX_THRESHOLDS.CAUTION_PPB} ppb</li>
        <li class="level-forecast">{OX_LEVEL_LABELS.forecast}: ≥ {OX_THRESHOLDS.CAUTION_PPB} ppb（要監視）</li>
        <li class="level-warning">{OX_LEVEL_LABELS.warning}: ≥ {OX_THRESHOLDS.WARNING_PPB} ppb（0.12 ppm）</li>
        <li class="level-alert">{OX_LEVEL_LABELS.alert}: ≥ {OX_THRESHOLDS.ALERT_PPB} ppb（0.24 ppm）</li>
        <li class="level-severe">{OX_LEVEL_LABELS.severe}: ≥ {OX_THRESHOLDS.SEVERE_PPB} ppb（0.40 ppm）</li>
      </ul>
    </div>
  </section>

  <section class="section weather-note">
    <h2>気象情報について</h2>
    <p>
      表中の「気温・湿度・風向・風速」は、測定局で気象を観測している場合のみ表示しています。
      多くの局では気象を測定していないため、発令判断には気象庁や他の気象データソースの併用を推奨します。
    </p>
    <p class="muted">
      <a href="https://www.env.go.jp/air/osen/pc_oxidant.html" target="_blank" rel="noopener">環境省・光化学オキシダント関連情報</a>
      ·
      <a href="https://soramame.env.go.jp/" target="_blank" rel="noopener">そらまめ君</a>
    </p>
  </section>
</main>

<style>
  .dashboard {
    max-width: 1400px;
    margin: 0 auto;
    padding: 1.5rem;
    font-family: 'Segoe UI', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', sans-serif;
    color: #1a1a1a;
    background: #f5f6f8;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #0d47a1 0%, #1565c0 100%);
    color: #fff;
    padding: 1.5rem 2rem;
    border-radius: 12px;
    margin-bottom: 1.5rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  }
  .header h1 { margin: 0 0 0.25rem 0; font-size: 1.6rem; font-weight: 700; }
  .subtitle { margin: 0 0 1rem 0; opacity: 0.9; font-size: 0.9rem; }
  .header-actions { display: flex; align-items: center; gap: 1rem; }
  .header-actions button {
    padding: 0.5rem 1rem; border-radius: 8px; border: none;
    background: rgba(255, 255, 255, 0.2); color: #fff; cursor: pointer; font-weight: 600;
  }
  .header-actions button:hover:not(:disabled) { background: rgba(255, 255, 255, 0.35); }
  .header-actions button:disabled { opacity: 0.7; cursor: not-allowed; }
  .updated { font-size: 0.85rem; opacity: 0.9; }
  .error {
    background: #ffebee; color: #c62828; padding: 1rem 1.5rem; border-radius: 8px; margin-bottom: 1.5rem;
  }
  .charts-row {
    margin-bottom: 1.5rem;
  }
  @media (min-width: 960px) {
    .charts-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .charts-row .section {
      min-width: 0;
      margin-bottom: 0;
    }
  }
  .section {
    background: #fff; border-radius: 12px; padding: 1.25rem 1.5rem; margin-bottom: 1.5rem;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  }
  .section h2 { margin: 0 0 0.75rem 0; font-size: 1.1rem; font-weight: 600; color: #333; }
  .table-legend { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid #eee; }
  .table-legend h3 { margin: 0 0 0.5rem 0; font-size: 0.95rem; font-weight: 600; color: #555; }
  .table-legend .thresholds { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 0.75rem 1.5rem; }
  .table-legend .thresholds li { padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.9rem; }
  .level-normal { background: #e8f5e9; color: #2e7d32; }
  .level-forecast { background: #fff8e1; color: #f57f17; }
  .level-warning { background: #ffe0b2; color: #e65100; }
  .level-alert { background: #ffccbc; color: #bf360c; }
  .level-severe { background: #ffcdd2; color: #b71c1c; }
  .simulate-note { margin: 0 0 0.25rem 0; font-size: 0.85rem; color: #e65100; font-weight: 600; }
  .target-datetime { margin: 0 0 0.75rem 0; font-size: 0.9rem; color: #555; }
  .table-wrap {
    overflow: auto;
    max-height: 320px;
  }
  .data-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  .data-table th, .data-table td { padding: 0.5rem 0.6rem; text-align: left; border-bottom: 1px solid #eee; }
  .data-table th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: #f5f5f5;
    font-weight: 600;
    color: #444;
    box-shadow: 0 1px 0 #eee;
  }
  .data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
  .data-table .ox { font-weight: 600; }
  .data-table tr.level-warning .ox { color: #e65100; }
  .data-table tr.level-alert .ox { color: #bf360c; }
  .data-table tr.level-severe .ox { color: #b71c1c; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
  .station-name { max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .map-datetime { margin: 0 0 0.5rem 0; font-size: 0.9rem; color: #555; }
  .map-legend { margin: 0.5rem 0 0; font-size: 0.8rem; color: #666; }
  .plotly-map-wrap {
    position: relative;
    height: 420px;
    min-height: 420px;
  }
  .plotly-map-wrap .map-gradient-img {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    object-fit: fill;
    pointer-events: none;
    z-index: 0;
  }
  .plotly-map-wrap .plotly-map {
    position: relative;
    z-index: 1;
    min-height: 420px;
  }
  .plotly-map-wrap .plotly-debug3 {
    margin-top: 1rem;
    min-height: 420px;
  }
  .section.timeseries {
    overflow: hidden;
    max-width: 100%;
  }
  .plotly-container {
    min-height: 420px;
    width: 100%;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    box-sizing: border-box;
  }
  .plotly-container :global(.plotly),
  .plotly-container :global(.svg-container) {
    max-width: 100% !important;
  }
  .weather-note p { margin: 0 0 0.5rem 0; font-size: 0.9rem; color: #555; }
  .muted { font-size: 0.85rem; color: #888; }
  .muted a { color: #1565c0; }
</style>
