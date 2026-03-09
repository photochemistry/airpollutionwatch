<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import Plotly from 'plotly.js-dist-min';
  import type { GridFieldResponse } from './api';
  import type { LatestRow, BBox } from './types';

  export let gridFieldData: GridFieldResponse | null = null;
  export let latestWithNames: LatestRow[] = [];
  export let outlineRings: [number, number][][] = [];
  export let bboxForMap: BBox = { minLon: 128, minLat: 30, maxLon: 146, maxLat: 46 };
  export let datetime: string | null = null;
  export let loading: boolean = false;
  export let prefName: string = '';
  export let oxDisplayMultiplier: number = 1;
  /** データ更新を通知するカウンター（親がインクリメントしてリドロー要求） */
  export let dataVersion: number = 0;

  const DEBUG_PLOT = false;
  const DEBUG_HEATMAP_SINCOS = false;

  let mapPlotDiv: HTMLDivElement | null = null;
  let debugPlot3Div: HTMLDivElement | null = null;
  let mapGradientDataUrl: string | null = null;

  const REF_PPB = 120;

  function tileXYToLonLat(x: number, y: number, zoom: number): [number, number] {
    const n = 2 ** zoom;
    const lon = (x / n) * 360 - 180;
    const latRad = Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n)));
    const lat = (180 / Math.PI) * latRad;
    return [lon, lat];
  }

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

  function oxToColor(ox: number | null, zMin = 0, zMax = 250): string {
    if (ox == null || !Number.isFinite(ox)) return 'rgb(180,180,180)';
    const t = Math.max(0, Math.min(1, (ox - zMin) / (zMax - zMin || 1)));
    const colors: [number, string][] = [
      [0, 'rgb(34,139,34)'],
      [0.25, 'rgb(76,175,74)'],
      [0.5, 'rgb(255,235,59)'],
      [0.75, 'rgb(255,152,0)'],
      [1, 'rgb(227,26,28)'],
    ];
    let i = 0;
    while (i + 1 < colors.length && colors[i + 1][0] < t) i++;
    const [t0, c0] = colors[i];
    const [t1, c1] = colors[i + 1] ?? colors[i];
    const u = (t - t0) / (t1 - t0 || 1);
    const parseRgb = (s: string) => {
      const m = s.match(/rgb\((\d+),(\d+),(\d+)\)/);
      return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : [128, 128, 128];
    };
    const [r0, g0, b0] = parseRgb(c0);
    const [r1, g1, b1] = parseRgb(c1);
    const r = Math.round(r0 + u * (r1 - r0));
    const g = Math.round(g0 + u * (g1 - g0));
    const b = Math.round(b0 + u * (b1 - b0));
    return `rgb(${r},${g},${b})`;
  }

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

  function makeKanagawaSinCosData(): { x: number[]; y: number[]; z: number[][] } {
    const nx = 40;
    const ny = 30;
    const { minLon, maxLon, minLat, maxLat } = bboxForMap;
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
        const v = Math.sin(x[j]) * Math.cos(y[i]);
        row.push((v + 1) * 125);
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
          x, y, z,
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
            x, y, z,
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
      : (gridFieldData ? gridToHeatmapData(gridFieldData, oxDisplayMultiplier) : null);
    mapGradientDataUrl = null;
    const traces: Record<string, unknown>[] = [];

    /** 等高線・ヒートマップのスケールはこの範囲にクランプ（1局の異常値で全国が崩れないように） */
    const MAP_OX_SCALE_MAX = 500;
    const oxValues = latestWithNames
      .map((r) => r.ox)
      .filter((v): v is number => v != null && Number.isFinite(v));
    const heatmapZFlat = heatmapData ? heatmapData.z.flat().filter((v): v is number => Number.isFinite(v)) : [];
    const allValues = [...oxValues, ...heatmapZFlat];
    const rawMin = allValues.length ? Math.min(...allValues) : 0;
    const rawMax = allValues.length ? Math.max(...allValues) : 250;
    const dataMin = Math.max(0, rawMin);
    const dataMax = Math.min(MAP_OX_SCALE_MAX, rawMax);
    const range = dataMax - dataMin || 1;
    const zMin = Math.max(0, dataMin - range * 0.1);
    const zMax = Math.min(MAP_OX_SCALE_MAX, dataMax + range * 0.1);

    if (rawMin < 0 || rawMax > MAP_OX_SCALE_MAX) {
      const outliers = latestWithNames.filter(
        (r) => r.ox != null && Number.isFinite(r.ox) && (r.ox < 0 || r.ox > MAP_OX_SCALE_MAX)
      );
      if (outliers.length > 0) {
        console.warn(
          '[drawMapPlotly] 等高線スケール外のOX値（異常値の可能性）:',
          outliers.map((r) => ({ station_id: r.station_id, name: r.name, ox: r.ox }))
        );
      }
    }

    for (const ring of outlineRings) {
      if (ring.length === 0) continue;
      traces.push({
        x: ring.map((c) => c[0]),
        y: ring.map((c) => c[1]),
        type: 'scatter',
        mode: 'lines',
        line: { color: '#000', width: 4 },
        fill: 'none',
        showlegend: false,
      });
    }

    let heatmapX: number[] = [];
    let heatmapY: number[] = [];
    let heatmapZ: number[][] = [];
    if (heatmapData) {
      heatmapX = heatmapData.x.slice();
      heatmapY = heatmapData.y.slice();
      heatmapZ = heatmapData.z.map((row) => row.slice());
      if (DEBUG_HEATMAP_SINCOS) {
        console.log('[drawMapPlotly] DEBUG_HEATMAP_SINCOS: Kanagawa xy + sin(x)*cos(y), x.len=', heatmapX.length, 'y.len=', heatmapY.length);
      } else {
        console.log('[drawMapPlotly] adding heatmap trace, zMin=', zMin.toFixed(0), 'zMax=', zMax.toFixed(0));
      }
      const colorscale: [number, string][] = [
        [0, 'rgba(34,139,34,0.75)'],
        [0.25, 'rgba(76,175,74,0.75)'],
        [0.5, 'rgba(255,235,59,0.75)'],
        [0.75, 'rgba(255,152,0,0.75)'],
        [1, 'rgba(227,26,28,0.75)'],
      ];
      traces.push({
        x: heatmapX,
        y: heatmapY,
        z: heatmapZ,
        type: 'heatmap',
        zmin: zMin,
        zmax: zMax,
        colorscale,
        colorbar: { title: 'OX (ppb)' },
      });
      const contourStart = Math.floor(zMin / 10) * 10;
      const contourEnd = Math.min(MAP_OX_SCALE_MAX, Math.ceil(zMax / 10) * 10);
      traces.push({
        x: heatmapX,
        y: heatmapY,
        z: heatmapZ,
        type: 'contour',
        zmin: zMin,
        zmax: zMax,
        contours: {
          start: contourStart,
          end: contourEnd,
          size: 10,
          showlabels: true,
          showlines: true,
        },
        colorscale: [[0, 'rgba(0,0,0,0)'], [1, 'rgba(0,0,0,0)']],
        showscale: false,
        line: { color: '#333', width: 1 },
        contours_coloring: 'lines',
        coloring: 'lines',
        showlegend: false,
      });
    } else {
      if (gridFieldData) {
        console.warn('[drawMapPlotly] no heatmapData ですが gridFieldData はあります。上記 [gridToHeatmapData] のログで原因を確認してください。');
      } else {
        console.log('[drawMapPlotly] no heatmapData (gridFieldData が null＝API 未取得または取得失敗)');
      }
    }
    const stationLons: number[] = [];
    const stationLats: number[] = [];
    const stationColors: string[] = [];
    for (const row of latestWithNames) {
      if (row.lat == null || row.lon == null || !Number.isFinite(row.lat) || !Number.isFinite(row.lon)) continue;
      stationLons.push(row.lon);
      stationLats.push(row.lat);
      stationColors.push(oxToColor(row.ox, zMin, zMax));
    }
    if (stationLons.length > 0) {
      traces.push({
        x: stationLons,
        y: stationLats,
        type: 'scatter',
        mode: 'markers',
        marker: {
          size: 10,
          symbol: 'circle',
          color: stationColors,
          line: { color: '#333', width: 1 },
        },
        showlegend: false,
      });
    }
    const plotHeight = Math.max(200, mapPlotDiv.parentElement?.clientHeight ?? 420);
    const layout = {
      xaxis: {
        title: '経度',
        range: [bboxForMap.minLon, bboxForMap.maxLon] as [number, number],
        constrain: 'domain',
      },
      yaxis: {
        title: '緯度',
        range: [bboxForMap.minLat, bboxForMap.maxLat] as [number, number],
        scaleanchor: 'x',
        scaleratio: 1,
      },
      margin: { t: 24, r: 24, b: 40, l: 52 },
      height: plotHeight,
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

  $: if (mapPlotDiv && dataVersion >= 0) {
    void gridFieldData;
    void latestWithNames.length;
    void outlineRings.length;
    drawMapPlotly();
  }

  let panelRoot: HTMLElement;
  let resizeObserver: ResizeObserver | null = null;

  onMount(() => {
    tick().then(() => {
      const observeTarget = mapPlotDiv?.parentElement ?? panelRoot;
      if (!observeTarget || !mapPlotDiv) return;
      resizeObserver = new ResizeObserver(() => {
        if (mapPlotDiv) drawMapPlotly();
      });
      resizeObserver.observe(observeTarget);
    });
  });

  onDestroy(() => {
    resizeObserver?.disconnect();
  });
</script>

<section class="section map-section" bind:this={panelRoot}>
  <h2>{prefName} OX 分布（補間・ヒートマップ）</h2>
  {#if datetime}
    <p class="map-datetime">対象時刻: {datetime}</p>
  {/if}
  {#if !gridFieldData && !loading}
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
  <p class="map-legend">階調: 低（緑）→ 高（赤）。ヒートマップは半透過、等高線は10ppb間隔。太線は県輪郭（dataofjapan/land）。〇は測定局。</p>
</section>

<style>
  .section {
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: #fff;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 0;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  }
  .section h2 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    font-weight: 600;
    color: #333;
  }
  .map-datetime {
    margin: 0 0 0.5rem 0;
    font-size: 0.9rem;
    color: #555;
  }
  .map-legend {
    margin: 0.5rem 0 0;
    font-size: 0.8rem;
    color: #666;
  }
  .plotly-map-wrap {
    position: relative;
    flex: 1 1 0;
    min-height: 200px;
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
    min-height: 200px;
    height: 100%;
  }
  .plotly-map-wrap .plotly-debug3 {
    margin-top: 1rem;
    min-height: 200px;
  }
  .plotly-container {
    min-height: 200px;
    height: 100%;
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
  .muted {
    font-size: 0.85rem;
    color: #888;
  }
</style>
