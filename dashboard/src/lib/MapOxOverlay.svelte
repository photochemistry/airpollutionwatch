<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import L from 'leaflet';
  import 'leaflet/dist/leaflet.css';
  import type { GridFieldResponse } from './api';

  const KANAGAWA_BBOX = { minLon: 138.9, minLat: 35.1, maxLon: 139.85, maxLat: 35.7 };
  const REF_PPB = 120;

  export let gridData: GridFieldResponse | null = null;
  export let datetimeLabel: string = '';
  /** 表示用倍率（地図の濃度に適用、1 で実値） */
  export let displayMultiplier: number = 1;

  let mapContainer: HTMLDivElement;
  let map: L.Map | null = null;
  let overlay: L.ImageOverlay | null = null;
  let dataRangeMin: number = 0;
  let dataRangeMax: number = REF_PPB;

  function tileXYToLonLat(x: number, y: number, zoom: number): [number, number] {
    const n = 2 ** zoom;
    const lon = (x / n) * 360 - 180;
    const latRad = Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n)));
    const lat = (180 / Math.PI) * latRad;
    return [lon, lat];
  }

  function valueToRgbaRelative(v: number | null, vMin: number, vMax: number): string {
    if (v == null || Number.isNaN(v)) return 'rgba(0,0,0,0)';
    const range = vMax - vMin;
    const t = range > 0 ? Math.max(0, Math.min(1, (v - vMin) / range)) : 0;
    const a = 0.7;
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

  function drawOverlay(data: GridFieldResponse): void {
    if (!map) return;
    const { values, tile_x_min, tile_x_max, tile_y_min, tile_y_max, z } = data;
    const nx = tile_x_max - tile_x_min + 1;
    const ny = tile_y_max - tile_y_min + 1;
    const mul = displayMultiplier;

    const getVal = (row: number, col: number): number | null => {
      const v = values[row]?.[col];
      if (v == null || Number.isNaN(v)) return null;
      return v * mul;
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
    dataRangeMin = vMin;
    dataRangeMax = vMax;

    const canvas = document.createElement('canvas');
    canvas.width = nx;
    canvas.height = ny;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    for (let row = 0; row < ny; row++) {
      for (let col = 0; col < nx; col++) {
        const val = getVal(row, col);
        ctx.fillStyle = valueToRgbaRelative(val, vMin, vMax);
        ctx.fillRect(col, row, 1, 1);
      }
    }

    const southWest = tileXYToLonLat(tile_x_min + 0.5, tile_y_max + 0.5, z);
    const northEast = tileXYToLonLat(tile_x_max + 0.5, tile_y_min + 0.5, z);
    const bounds = L.latLngBounds(
      [southWest[1], southWest[0]],
      [northEast[1], northEast[0]]
    );
    if (overlay) map.removeLayer(overlay);
    overlay = L.imageOverlay(canvas.toDataURL('image/png'), bounds, { opacity: 0.85 });
    overlay.addTo(map);
  }

  $: if (map && gridData) {
    void displayMultiplier;
    drawOverlay(gridData);
  }

  onMount(() => {
    map = L.map(mapContainer).setView(
      [(KANAGAWA_BBOX.minLat + KANAGAWA_BBOX.maxLat) / 2, (KANAGAWA_BBOX.minLon + KANAGAWA_BBOX.maxLon) / 2],
      10
    );
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    map.fitBounds([
      [KANAGAWA_BBOX.minLat, KANAGAWA_BBOX.minLon],
      [KANAGAWA_BBOX.maxLat, KANAGAWA_BBOX.maxLon],
    ]);
    if (gridData) drawOverlay(gridData);
  });

  onDestroy(() => {
    if (overlay && map) map.removeLayer(overlay);
    if (map) map.remove();
    map = null;
    overlay = null;
  });
</script>

<div class="map-section">
  {#if datetimeLabel}
    <p class="map-datetime">対象時刻: {datetimeLabel}</p>
  {/if}
  <div class="map-wrap">
    <div class="map-leaflet" bind:this={mapContainer}></div>
  </div>
  <p class="map-legend">
    階調: 低（緑）→ 高（赤）
    {#if gridData && dataRangeMin !== dataRangeMax}
      — 表示範囲: <strong>{dataRangeMin.toFixed(0)} ～ {dataRangeMax.toFixed(0)} ppb</strong>
    {/if}
  </p>
</div>

<style>
  .map-section { margin-top: 0.5rem; }
  .map-datetime { margin: 0 0 0.5rem 0; font-size: 0.9rem; color: #555; }
  .map-wrap {
    position: relative;
    width: 100%;
    height: 420px;
    border-radius: 8px;
    overflow: hidden;
    background: #e0e0e0;
  }
  .map-leaflet {
    width: 100%;
    height: 100%;
  }
  .map-wrap :global(.leaflet-container) { font-family: inherit; }
  .map-legend {
    margin: 0.5rem 0 0 0;
    font-size: 0.8rem;
    color: #666;
  }
  .leg { padding: 0 0.25rem; }
  .leg.normal { color: #2e7d32; }
  .leg.caution { color: #f57f17; }
  .leg.warning { color: #e65100; }
  .leg.alert { color: #b71c1c; }
</style>
