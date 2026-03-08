# 神奈川県 光化学オキシダント 監視ダッシュボード

大気管理者向けの監視ダッシュボードです。[airpollutionwatch API](https://github.com/photochemistry/airpollutionwatch) のデータを使い、神奈川県の光化学オキシダント（OX）を中心に最新値・本日の推移・注意報基準を一覧表示します。

## 主な機能

- **最新の測定値**: 県内の OX 測定局を OX 値の高い順に表示。注意報（≥120 ppb）・警報（≥240 ppb）・重大警報（≥400 ppb）で色分け。
- **NOx / NO2**: 光化学オキシダントの前駆物質として併記。
- **気象**: 測定局で気象（気温・湿度・風向・風速）を観測している局のみ表示。多くの局では未測定のため、発令判断には気象庁等の別ソースの併用を推奨。
- **本日の OX 推移**: 局ごとの 1 時間値の簡易バーチャート。
- **注意報・警報の基準**: 環境省の基準（ppb / ppm）を凡例で表示。

## 技術スタック

- [Svelte](https://svelte.dev/) 4 + [Vite](https://vitejs.dev/) 5
- TypeScript
- スタンドアロンの SPA（静的ビルドで任意のホストに配置可能）

## セットアップ

```bash
cd dashboard
npm install
```

## 環境変数

| 変数名 | 説明 |
|--------|------|
| `VITE_API_BASE_URL` | airpollutionwatch API のベース URL。未設定時は `https://andersan.net:8089` を使用。 |

例（ローカルの API を使う場合）:

```bash
# .env を作成
echo 'VITE_API_BASE_URL=http://localhost:8089' > .env
```

## 開発

```bash
npm run dev
```

ブラウザで http://localhost:5173 を開きます。

## ビルド

```bash
npm run build
```

`dist/` に静的ファイルが出力されます。任意の Web サーバで `dist` を配信してください。

## 注意事項

- 気象（気温・湿度・風向・風速）は、大気測定局で観測している局のみ API から取得できます。多くの局では測定していないため、表中は「—」になります。発令判断には気象庁や他の気象データソースの併用を推奨します。
- 光化学オキシダント注意報の発令基準は「1 時間値 0.12 ppm（120 ppb）以上で、気象条件からみてその濃度が継続すると認められるとき」です。本ダッシュボードは参考表示であり、発令は各都道府県の手順に従ってください。

## 参考リンク

- [環境省・光化学オキシダント関連情報](https://www.env.go.jp/air/osen/pc_oxidant.html)
- [そらまめ君（大気汚染物質広域監視システム）](https://soramame.env.go.jp/)
