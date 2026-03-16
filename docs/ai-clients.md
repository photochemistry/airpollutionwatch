# airpollutionwatch API — AI クライアント向け利用ガイド

このドキュメントは、ChatGPT / Cursor などの **LLM クライアントが `airpollutionwatch` API を呼び出す** ことを想定したガイドです。  
人間向けの説明は `README.md` や Swagger UI（`/docs`）を参照してください。

- **ベース URL（本番）**: `https://andersan.net:8089`
- **API バージョン**: 現行は **v1**（すべて `/v1/...` 配下）

---

## 1. API の目的と全体像

- 日本全国の都道府県が公開している **大気環境データ（1 時間ごと）** を収集し、  
  **共通スキーマ（そらまめ互換カラム名）で提供** する Web API。
- 主なユースケース:
  - 「特定の局の PM2.5 時系列グラフを描く」
  - 「都道府県ごとの最新 PM2.5 を地図に塗る」
  - 「観測局一覧を地図上に表示する」

LLM クライアントから使う場合は、**次の 3 系統のエンドポイント**を組み合わせるとよいです。

- **マスタ系**: `/v1/prefectures`, `/v1/stations`, `/v1/stations/{station_id}`
- **観測値系**: `/v1/measurements`, `/v1/latest`
- **メタ / 監視系**: `/v1/coverage`, `/v1/log`（JSON: 県別ステータス + ログ本文）

`/v1/measurements` のレスポンス形式の詳細は  
`docs/measurements-response-formats.md` もあわせて参照してください。

---

## 2. 主要な概念（LLM が理解しておくべき用語）

- **prefecture ID (`pref`)**
  - 都道府県を表す文字列 ID（例: `tokyo`, `aichi`）。  
  - `GET /v1/prefectures` の `id` フィールドで取得できる。
- **station_id**
  - 国環研の局番号（8 桁の数字文字列。先頭 0 は省略可能）。  
  - `GET /v1/stations` / `GET /v1/stations/{station_id}` で取得・確認できる。
- **target_datetime / observed_datetime**
  - `target_datetime`: 収集対象とした「正時」（丸められた 1 時間ごとの基準時刻）。
  - `observed_datetime`: 実際の観測値の時刻。  
    データが欠けている場合などに **1 時間フォールバック** が行われることがある。
- **pollutants / 測定項目**
  - `pm25, ox, no2, ...` のような名前で指定。  
  - 実際のカラム名は `PM25`, `OX`, `NO2` などの **そらまめ互換列名** になる。
- **format=series / snapshot（`/v1/measurements`）**
  - `series`: (局, 測定項目) ごとの **時系列配列**。
  - `snapshot`: 1 時刻の **局ごとの一覧（1 局 1 オブジェクト）**。

---

## 3. エンドポイント選択の指針

### 3.1 都道府県一覧・メタデータ

- **都道府県マスタ**
  - `GET /v1/prefectures`
  - 使いどころ:
    - ユーザーに選ばせる都道府県一覧を出すとき
    - `pref` パラメータに使える ID を知りたいとき

- **局メタデータ一覧**
  - `GET /v1/stations?pref={pref}&has=pm25,ox`
  - 使いどころ:
    - 地図に載せる観測局の一覧を取得
    - 「PM2.5 を測定している局だけ欲しい」などのフィルタ

- **局詳細**
  - `GET /v1/stations/{station_id}`
  - 使いどころ:
    - グラフや詳細画面のラベルに住所・局種別などを表示したいとき

### 3.2 観測データの取得

- **任意期間の時系列**
  - `GET /v1/measurements`
  - 主なクエリ:
    - `station_ids`: カンマ区切りの局番号（例: `13114010,13114510`）
    - `pref`: 都道府県 ID（指定するとその県の全局が対象）
    - `from`, `to`: 期間（ISO8601、1 時間単位で解釈）
    - `pollutants`: `pm25,ox,no2` など
    - `format`: `series`（既定）または `snapshot`
  - **こういうときに使う**:
    - 「特定局の 1 週間の PM2.5 グラフ」
    - 「ある県の複数局の NO2 時系列を比較」

- **最新値（1 時刻）**
  - `GET /v1/latest`
  - 主なクエリ:
    - `station_ids` または `pref`（どちらか一方）
    - `pollutants`
  - **こういうときに使う**:
    - 「東京の全局の現在の PM2.5 を一覧表示」
    - 「特定の 1 局の現在値だけ知りたい」

### 3.3 監視 / メタ情報

- **どこまで履歴が埋まっているか**
  - `GET /v1/coverage`（HTML）
- **収集ジョブログ**
  - `GET /v1/log`（JSON: 県別ステータス `status_items` と `collect_log` 本文）

AI エージェントが自動運用を補助する場合、  
「データがないことによるエラー」かどうか判断するために `/v1/coverage` や `/v1/log` を併用するとよいです。

---

## 4. 典型タスク別レシピ（LLM 向け）

### 4.1 「特定局の PM2.5 の 1 週間時系列を取得してグラフにしたい」

1. 入力として局番号（station_id）と期間（開始日時 / 終了日時）を受け取る。
2. 次のように `GET /v1/measurements` を呼び出す。
   - `station_ids={station_id}`
   - `from={開始日時}`
   - `to={終了日時}`
   - `pollutants=pm25`
   - `format=series`
3. レスポンスの `timeseries[0].values` を時系列配列として利用する。

### 4.2 「都道府県ごとの最新 PM2.5 マップを作りたい」

1. `GET /v1/prefectures` で都道府県 ID の一覧を取得する。
2. 各都道府県について:
   - `GET /v1/latest?pref={pref}&pollutants=pm25`
   - `stations` 配列から PM2.5 値を取得し、平均または代表値を計算する。
3. 計算結果を都道府県ポリゴンにマッピングして可視化する。

### 4.3 「ユーザーが都道府県名（日本語）で指定してきたとき」

1. `GET /v1/prefectures` の `name_ja` から、最も近い日本語名を選ぶ（完全一致が理想）。  
2. 対応する `id` を `pref` クエリに使う。

---

## 5. LLM クライアント実装のベストプラクティス

- **必ずバリデーションエラーをチェックする**
  - `400 Bad Request` の場合、`detail` メッセージからどのパラメータが不正かを解析し、  
    プロンプト内で自分の呼び出しロジックを修正する。
- **`station_ids` と `pref` は同時に指定しない**
  - どちらか一方のみ許可されている（`/v1/measurements`, `/v1/latest` 共通）。
- **`format=snapshot` のときは `from` と `to` を同一正時にする**
  - 仕様上の制約。異なると 400 エラーになる。
- **タイムゾーンは ISO8601 で明示する**
  - 例: `2024-09-03T06:00:00+09:00`  
  - ユーザーの自然言語（「今日の 9 時」など）を JST などに正規化してからクエリに使う。

---

## 6. 人間向けドキュメントとの関係

- このファイルは **「LLM から見た使い方」** をまとめた補助ドキュメントです。
- より詳細な仕様やサンプルは、以下もあわせて参照してください。
  - `README.md`（日本語の全体ドキュメント）
  - `docs/measurements-response-formats.md`（`/v1/measurements` のレスポンス形式）
  - Swagger UI（`/docs`）

LLM からこの API を使うときは、まずこのガイドを読み込み、  
必要に応じて他のドキュメントを補助的に参照する構成を想定しています。

