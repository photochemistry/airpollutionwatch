# airpollutionwatch API ドキュメント（日本語）

`airpollutionwatch` は、日本の各都道府県が公開している大気環境データを 1 時間ごとに収集し、  
SQLite データベースおよび Web API から参照できるようにするプロジェクトです。

ここでは **HTTP API（FastAPI）として利用する場合**の説明をまとめます。  
内部実装やスクレイパの詳細は `INTERNALS.md` を参照してください。

---

## API の概要

- **ベース URL**: `http://<ホスト>:8089`（開発時のデフォルト）
- **API ドキュメント**: `http://<ホスト>:8089/docs`  
  FastAPI 標準の Swagger UI が表示されます（説明文は日本語）。

提供する主な機能:

- 都道府県ごとの大気環境データ（1 時間ごと）の取得
- 測定項目の単位・意味などの仕様情報の取得
- どこまで過去の時刻まで連続データがあるかの可視化
- バックグラウンド収集ジョブのログ閲覧

---

## 起動方法

### 1. 依存パッケージのインストール

```bash
poetry install
```

または、`pyproject.toml` の依存関係に従って `pip` でインストールしてください。

### 2. API サーバの起動

```bash
poetry run python api.py
```

デフォルトでは `http://0.0.0.0:8089` で待ち受けます。  
ブラウザで `http://localhost:8089/docs` にアクセスすると、Swagger UI が利用できます。

---

## エンドポイント一覧

### `GET /prefectures`

- **説明**: 取得可能な都道府県 ID の一覧を返します。
- **用途**:
  - `/items/{prefecture}/{datehour}` を叩く前に、有効な `prefecture` 値を確認したいとき。
- **レスポンス例**:

```json
["hokkaido", "aomori", "iwate", "miyagi", "akita", "..."]
```

ここで返る文字列を、そのまま `{prefecture}` パスパラメータとして使用します。

---

### `GET /items/{prefecture}/{datehour}`

- **説明**: 指定した都道府県・時刻の大気環境データを返します。
- **パスパラメータ**:
  - `prefecture`  
    都道府県 ID（例: `"tokyo"`, `"kanagawa"`, `"hokkaido"` など）。  
    値は `/prefectures` のレスポンスに含まれるキーのみ有効です。
  - `datehour`  
    ISO 8601 形式の日時（例: `"2024-09-03T06:00+09:00"`）。  
    分以下は切り捨てられ、内部では正時に丸められます。

- **フォールバック動作**:
  - 指定した正時 `datehour` に対応する `target_datetime` のデータがなければ、
    自動的に **1 時間前** の `target_datetime` までフォールバックして検索します。
  - それでも見つからない場合は `404 Not Found` になります。

- **レスポンスの構造**（概略）:

```json
{
  "data": {
    "station_code": { "0": 13114010, "1": 13114510 },
    "observed_datetime": {
      "0": "2024-09-03T06:00:00+09:00",
      "1": "2024-09-03T06:00:00+09:00"
    },
    "SO2": { "0": 1.2, "1": 0.8 },
    "NO":  { "0": 5.1, "1": 3.4 },
    "NO2": { "0": 8.0, "1": 6.5 },
    "PM25": { "0": 12.0, "1": 15.3 },
    "...": {}
  },
  "spec": {
    "SO2": {
      "name": "SO2",
      "unit": "ppb",
      "description": "硫黄酸化物",
      "type": "float",
      "default": 0.0
    },
    "PM25": {
      "name": "PM25",
      "unit": "ug/m3",
      "description": "PM2.5",
      "type": "float",
      "default": 0.0
    }
  }
}
```

- `data` セクション  
  - 各キー（`SO2`, `NO2`, `PM25`, `station_code`, `observed_datetime` など）は  
    「内部行番号 → 値」の辞書になっています。
  - `station_code` と `observed_datetime` を組み合わせることで、  
    「どの測定局の・いつの値か」を特定できます。

- `spec` セクション  
  - 各測定項目について、単位・意味・型・デフォルト値を含むメタ情報を返します。
  - グラフ描画や UI ラベル表示などで利用できます。

#### 簡単な利用例（curl）

```bash
curl "http://localhost:8089/items/tokyo/2024-09-03T06:00:00%2B09:00"
```

---

### `GET /coverage`

- **説明**: 各都道府県ごとに、どこまで過去にさかのぼって連続データがあるかを HTML テーブルで返します。
- **詳細**:
  - `measurements` テーブルから県別の `target_datetime` を集計し、
  - もっとも新しい時刻から 1 時間ずつさかのぼって「途切れずに存在する最古の時刻」を求めます。
  - 現在の正時から見た日数差も計算して表示します。
- **用途**:
  - どの県の履歴がどのくらい埋まっているかの可視化。
  - cron やスクレイパの不調により、どこかの県だけ欠けていないかをブラウザでざっと確認したいとき。

ブラウザで `http://<ホスト>:8089/coverage` にアクセスしてください。

---

### `GET /collect.log`

- **説明**: 収集ジョブのログファイル（`collect.log`）の中身をプレーンテキストで返します。
- **用途**:
  - `collect_hourly.py` を cron 等で 5 分ごとに動かしている場合の成否確認。
  - どの県でどのようなエラーが発生しているかを一次チェックしたいとき。

`curl` での例:

```bash
curl "http://localhost:8089/collect.log"
```

---

### `GET /log`

- **説明**: `collect.log` をブラウザで閲覧するための簡易 HTML ビューを返します。
  - クライアント側 JavaScript により、5 分ごとに自動で `/collect.log` を再取得します。
- **用途**:
  - ターミナルを開かずに、ブラウザから常時ログをモニタしたい場合。

ブラウザで `http://<ホスト>:8089/log` にアクセスしてください。

---

## Python ライブラリとしての利用（簡単な例）

HTTP API ではなく Python から直接使いたい場合は、従来通り県別モジュールの `retrieve` を呼び出せます。

```python
from airpollutionwatch import tokyo

df = tokyo.retrieve("2024-08-08T23:00+09:00", station_set="air")
print(df.head())
```

- 返り値は `pandas.DataFrame` で、カラム名・単位はそらまめ君互換です。
- より詳しい内部構成や変換ロジックは `INTERNALS.md` を参照してください。

---

## 注意事項

- 各都道府県のウェブサイト仕様変更により、突然データ取得に失敗することがあります。  
  `/collect.log` や `/coverage` を併用して状態を確認してください。
- 00 時ちょうどのデータが存在しない県については、前日 24 時の値に読み替えて扱うなど、  
  県ごとの仕様に合わせた処理が各モジュール側に実装されています。

---

## 開発・貢献

- バグ報告や機能追加の提案は Issue / Pull Request で歓迎します。
- 内部実装の詳細や県別モジュールの作り方は `INTERNALS.md` にまとめています。

# airpollutionwatch - 日本の都道府県別大気汚染データ取得ライブラリ

`airpollutionwatch` は、日本の各都道府県が公開している大気汚染に関するデータを取得し、統一された形式で利用できるようにするための Python ライブラリです。このライブラリを使用することで、各都道府県のウェブサイトからデータをスクレイピングする手間を省き、簡単に大気汚染データを分析・活用することができます。

## 特徴

*   **都道府県別データ取得:** 各都道府県のウェブサイトから大気汚染データを取得する機能を提供します。
*   **データ形式の統一:** 各都道府県で異なるデータ形式を、`そらまめ君` のデータ形式に統一します。
*   **測定局と測定量の管理:** 測定局と測定量のコードと名称の対応を管理します。
*   **キャッシュ機能:** `requests-cache` を使用して、ウェブからのデータ取得をキャッシュし、効率化を図ります。
*   **単位変換:** 各都道府県で異なる単位を、`そらまめ君` の単位に変換します。
* **データ取得の自動化:** 指定した日時（ISO 8601形式）のデータを自動的に取得します。
* **測定局の絞り込み:** `station_set="air"` を指定することで、大気測定局（8桁の局番を持つもの）のみに絞り込むことができます。

## ファイル構成

`airpollutionwatch` フォルダには、以下の Python ファイルが含まれています。

*   `__init__.py`: パッケージの初期化ファイル。
*   `convert.py`: 測定量（SO2, NO, NO2, etc.）の単位変換や、測定局名の変換を行うための関数を定義しています。`そらまめ君` のデータ形式に合わせるための処理が記述されています。
*   `TM20210000.py`: 国環研の測定局情報（`STATIONS`）を定義しています。
*   `tokyo.py`: 東京都の大気汚染データを取得するためのモジュールです。
*   `kanagawa.py`: 神奈川県の大気汚染データを取得するためのモジュールです。
*   `shizuoka.py`: 静岡県の大気汚染データを取得するためのモジュールです。
*   `chiba.py`: 千葉県の大気汚染データを取得するためのモジュールです。
*   `yamanashi.py`: 山梨県の大気汚染データを取得するためのモジュールです。

## モジュールの説明

### `__init__.py`

*   パッケージの初期化ファイルです。現在は `numpy` をインポートしているのみです。

### `convert.py`

*   `station_to_id(station, aliases=None)`: 測定局名から国環研局番を取得します。
*   `PPB(series, unit="ppb")`: ppb 単位に変換します。
*   `dPPM(series, unit="0.1ppm")`: 0.1ppm 単位に変換します。
*   `UG_M3(series, unit="ug/m3")`: ug/m3 単位に変換します。
*   `dM_S(series, unit="0.1m/s")`: 0.1m/s 単位に変換します。
*   `DPPBC(series, unit="10ppbC")`: 10ppbC 単位に変換します。
*   `CELSIUS(series, unit="0.1celsius")`: 0.1celsius 単位に変換します。
*   `DEGREE(series, unit="degree")`: degree 単位に変換します。
*   `NOP(series)`: 数値に変換します。
*   `PERCENT(series, unit="%")`: % 単位に変換します。
*   `STATION(series, aliases)`: 測定局名を国環研局番に変換します。
*   `DIRC16(series, unit="16dirc")`: 風向を16方位コードに変換します。
*   `SO2(series, unit="ppb")`: SO2 の単位変換を行います。
*   `NO(series, unit="ppb")`: NO の単位変換を行います。
*   `NO2(series, unit="ppb")`: NO2 の単位変換を行います。
*   `NOX(series, unit="ppb")`: NOX の単位変換を行います。
*   `OX(series, unit="ppb")`: OX の単位変換を行います。
*   `CO(series, unit="0.1ppm")`: CO の単位変換を行います。
*   `NMHC(series, unit="10ppbC")`: NMHC の単位変換を行います。
*   `CH4(series, unit="10ppbC")`: CH4 の単位変換を行います。
*   `THC(series, unit="10ppbC")`: THC の単位変換を行います。
*   `WD(series, unit="16dirc")`: WD の単位変換を行います。
*   `WS(series, unit="0.1m/s")`: WS の単位変換を行います。
*   `TEMP(series, unit="celsius")`: TEMP の単位変換を行います。
*   `HUM(series, unit="%")`: HUM の単位変換を行います。
*   `SPM(series, unit="ug/m3")`: SPM の単位変換を行います。
*   `PM25(series, unit="ug/m3")`: PM25 の単位変換を行います。
*   `LON(series, unit="degree")`: 経度を変換します。
*   `LAT(series, unit="degree")`: 緯度を変換します。
*   `CODE(series)`: コードを変換します。
*   `test()`: テスト関数です。

### `TM20210000.py`

*   国環研の測定局情報（`STATIONS`）を定義しています。このデータは、`convert.py` で使用されます。

### `tokyo.py`, `kanagawa.py`, `shizuoka.py`, `chiba.py`, `yamanashi.py`

*   各都道府県の大気汚染データを取得するためのモジュールです。
*   `aliases`: ウェブ上の表記と国環研の表記との対応を定義します。
*   `converters`: ウェブ上のデータと `convert.py` の変換関数の対応を定義します。
*   `stations()`: 測定局情報を取得します。
*   `items()`: 測定量情報を取得します。
*   `retrieve_raw(isotime)`: 指定された日時の生データを取得します。
*   `retrieve(isotime, station_set="full")`: 指定された日時のデータを取得し、`そらまめ君` の形式に変換します。`station_set` で測定局を絞り込むことができます。
*   `test()`: テスト関数です。

## 使用方法

1.  必要な都道府県のモジュールをインポートします。
2.  `retrieve(isotime, station_set="full")` 関数を呼び出し、データを取得します。
    *   `isotime`: 取得したい日時を ISO 8601 形式で指定します（例: "2024-08-08T23:00+09:00"）。
    *   `station_set`: 測定局を絞り込むかどうかを指定します。デフォルトは "full"（すべての測定局）です。"air" を指定すると大気測定局のみになります。

```python
from airpollutionwatch import tokyo

# 2024年8月8日23時の東京都のデータを取得
df = tokyo.retrieve("2024-08-08T23:00+09:00")
print(df)

# 2024年8月8日23時の東京都の大気測定局のデータを取得
df_air = tokyo.retrieve("2024-08-08T23:00+09:00", station_set="air")
print(df_air)
```

## 依存ライブラリ

- pandas
- requests-cache
- numpy

## 注意事項

- 各都道府県のウェブサイトの仕様変更により、データ取得が正常に行えなくなる可能性があります。
- データ取得の頻度によっては、ウェブサイトに負荷をかける可能性があります。適切な間隔を空けてアクセスするようにしてください。
- 00時のデータは存在しないため、前日の24時のデータに自動的に変換されます。

## 今後の課題

- 対応都道府県の追加
- エラー処理の強化
- データ取得の安定化
- ドキュメントの充実

## 貢献

バグ報告や機能追加の提案など、貢献を歓迎します。


