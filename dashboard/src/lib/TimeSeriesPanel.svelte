<script lang="ts">
  import Plotly from 'plotly.js-dist-min';
  import type { OxSeriesItem } from './types';

  export let oxSeriesByStation: OxSeriesItem[] = [];
  export let oxDisplayMultiplier: number = 1;

  const OX_REFERENCE_PPB = 120;

  let plotDiv: HTMLDivElement | null = null;

  function drawPlotly() {
    if (!plotDiv || oxSeriesByStation.length === 0) return;
    const traces = oxSeriesByStation.map((series) => ({
      x: series.values.map((p) => p.datetime),
      y: series.values.map((p) => (p.value != null ? p.value * oxDisplayMultiplier : null)),
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
</script>

<section class="section timeseries">
  <h2>過去24時間の OX 推移（1時間値）</h2>
  {#if oxSeriesByStation.length > 0}
    <div class="plotly-container" bind:this={plotDiv}></div>
  {:else}
    <p class="muted">時系列データがありません（過去24時間のデータまたはAPI未取得）</p>
  {/if}
</section>

<style>
  .section {
    background: #fff;
    border-radius: 12px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 1.5rem;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
    overflow: hidden;
    max-width: 100%;
  }
  .section h2 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    font-weight: 600;
    color: #333;
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
  .muted {
    font-size: 0.85rem;
    color: #888;
  }
</style>
