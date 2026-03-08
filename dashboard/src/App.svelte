<script lang="ts">
  import { onMount } from 'svelte';
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
  import MapOxOverlay from './lib/MapOxOverlay.svelte';

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
  let gridFieldData: import('./lib/api').GridFieldResponse | null = null;
  const KANAGAWA_BBOX = '138.9,35.1,139.85,35.7';

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
          gridFieldData = await fetchGridField(KANAGAWA_BBOX, 'ox', latestRes.datetime, 12, 'atps', '0.007');
        } catch {
          gridFieldData = null;
        }
      } else {
        gridFieldData = null;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
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

  onMount(() => { load(); });
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
      <h2>神奈川県 OX 分布（補間マップ）</h2>
      {#if latest && !gridFieldData && !loading}
        <p class="muted">グリッドデータを取得できませんでした（API /v1/grid/field を確認してください）。</p>
      {/if}
      <MapOxOverlay gridData={gridFieldData} datetimeLabel={latest?.datetime ?? ''} displayMultiplier={OX_DISPLAY_MULTIPLIER} />
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
