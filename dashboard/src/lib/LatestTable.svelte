<script lang="ts">
  import { OX_THRESHOLDS, OX_LEVEL_LABELS } from './constants';
  import { levelClass, formatNum } from './utils';
  import type { LatestRow } from './types';

  export let latestWithNames: LatestRow[] = [];
  export let datetime: string | null = null;
  export let oxDisplayMultiplier: number = 1;
</script>

<section class="section latest">
  <h2>最新の測定値（OX 高い順）</h2>
  {#if oxDisplayMultiplier !== 1}
    <p class="simulate-note">※ OX 値はシミュレーション表示（実値×{oxDisplayMultiplier}）</p>
  {/if}
  {#if datetime}
    <p class="target-datetime">対象時刻: {datetime}</p>
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
    flex-shrink: 0;
  }
  .simulate-note {
    margin: 0 0 0.25rem 0;
    font-size: 0.85rem;
    color: #e65100;
    font-weight: 600;
    flex-shrink: 0;
  }
  .target-datetime {
    margin: 0 0 0.75rem 0;
    font-size: 0.9rem;
    color: #555;
    flex-shrink: 0;
  }
  .table-wrap {
    flex: 1 1 0;
    min-height: 0;
    overflow: auto;
  }
  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  .data-table th,
  .data-table td {
    padding: 0.5rem 0.6rem;
    text-align: left;
    border-bottom: 1px solid #eee;
  }
  .data-table th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: #f5f5f5;
    font-weight: 600;
    color: #444;
    box-shadow: 0 1px 0 #eee;
  }
  .data-table .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .data-table .ox {
    font-weight: 600;
  }
  .data-table tr.level-warning .ox { color: #e65100; }
  .data-table tr.level-alert .ox { color: #bf360c; }
  .data-table tr.level-severe .ox { color: #b71c1c; }
  .badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .station-name {
    max-width: 12rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .table-legend {
    flex-shrink: 0;
    margin-top: 1rem;
    padding-top: 0.75rem;
    border-top: 1px solid #eee;
  }
  .table-legend h3 {
    margin: 0 0 0.5rem 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: #555;
  }
  .table-legend .thresholds {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem 1.5rem;
  }
  .table-legend .thresholds li {
    padding: 0.25rem 0.5rem;
    border-radius: 6px;
    font-size: 0.9rem;
  }
  .level-normal { background: #e8f5e9; color: #2e7d32; }
  .level-forecast { background: #fff8e1; color: #f57f17; }
  .level-warning { background: #ffe0b2; color: #e65100; }
  .level-alert { background: #ffccbc; color: #bf360c; }
  .level-severe { background: #ffcdd2; color: #b71c1c; }
</style>
