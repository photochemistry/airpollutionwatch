<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import {
    Chart,
    LineController,
    LineElement,
    PointElement,
    LinearScale,
    TimeScale,
    Tooltip,
    Legend,
  } from 'chart.js';
  import 'chartjs-adapter-date-fns';
  import type { OxSeriesItem } from './types';

  export let oxSeriesByStation: OxSeriesItem[] = [];
  export let oxDisplayMultiplier: number = 1;

  const OX_REFERENCE_PPB = 120;

  Chart.register(
    LineController,
    LineElement,
    PointElement,
    LinearScale,
    TimeScale,
    Tooltip,
    Legend,
  );

  let canvasEl: HTMLCanvasElement | null = null;
  let chart: Chart<'line'> | null = null;
  let panelRoot: HTMLElement;
  let resizeObserver: ResizeObserver | null = null;

  function buildChartConfig() {
    const datasets = oxSeriesByStation.map((series, index) => {
      const colorPalette = [
        '#1f77b4',
        '#ff7f0e',
        '#2ca02c',
        '#d62728',
        '#9467bd',
        '#8c564b',
        '#e377c2',
        '#7f7f7f',
        '#bcbd22',
        '#17becf',
      ];
      const color = colorPalette[index % colorPalette.length];
      return {
        label: series.name,
        data: series.values.map((p) => ({
          x: p.datetime,
          y: p.value != null ? p.value * oxDisplayMultiplier : null,
        })),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 1.5,
        pointRadius: 0,
        spanGaps: false,
      };
    });

    const options: import('chart.js').ChartOptions<'line'> = {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          type: 'time',
          time: {
            unit: 'hour',
          },
          title: {
            display: true,
            text: '時刻（過去24時間）',
          },
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: 'OX (ppb)',
          },
        },
      },
      plugins: {
        legend: {
          display: true,
          position: 'top',
        },
        tooltip: {
          mode: 'nearest',
          intersect: false,
        },
        annotation: undefined,
      },
      elements: {
        line: {
          tension: 0,
        },
      },
    };

    return {
      type: 'line' as const,
      data: { datasets },
      options,
    };
  }

  function drawChart() {
    if (!canvasEl || oxSeriesByStation.length === 0) return;
    const ctx = canvasEl.getContext('2d');
    if (!ctx) return;

    const config = buildChartConfig();

    if (chart) {
      chart.data = config.data;
      chart.options = config.options;
      chart.update();
    } else {
      chart = new Chart(ctx, config);
    }
  }

  $: if (canvasEl && oxSeriesByStation.length > 0) {
    drawChart();
  }

  onMount(() => {
    tick().then(() => {
      const observeTarget = canvasEl?.parentElement ?? panelRoot;
      if (!observeTarget || !canvasEl) return;
      resizeObserver = new ResizeObserver(() => {
        if (!canvasEl) return;
        drawChart();
      });
      resizeObserver.observe(observeTarget);
    });
  });

  onDestroy(() => {
    resizeObserver?.disconnect();
    chart?.destroy();
    chart = null;
  });
</script>

<section class="section timeseries" bind:this={panelRoot}>
  <h2>過去24時間の OX 推移（1時間値）</h2>
  {#if oxSeriesByStation.length > 0}
    <div class="chartjs-container">
      <canvas bind:this={canvasEl}></canvas>
    </div>
  {:else}
    <p class="muted">時系列データがありません（過去24時間のデータまたはAPI未取得）</p>
  {/if}
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
    overflow: hidden;
    max-width: 100%;
  }
  .section h2 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    font-weight: 600;
    color: #333;
    flex-shrink: 0;
  }
  .chartjs-container {
    flex: 1 1 0;
    min-height: 200px;
    width: 100%;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    box-sizing: border-box;
    position: relative;
  }
  .chartjs-container canvas {
    width: 100% !important;
    height: 100% !important;
    display: block;
  }
  .muted {
    font-size: 0.85rem;
    color: #888;
  }
</style>
