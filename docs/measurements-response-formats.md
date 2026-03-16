# GET /measurements のレスポンス形式（series と snapshot）

`/measurements` は `format` クエリでレスポンスの形を切り替えます。どちらも同じ「局×測定項目×時刻」のデータで、**多次元配列の軸の取り方を変えているだけ**です。

## 軸の取り方

- **format=series**（既定）  
  - 外側の軸: **(局, 測定項目)** の組。  
  - 各要素の `values` が時刻の配列。  
  - from=to のときは各 `values` の長さは 1。

- **format=snapshot**  
  - 外側の軸: **局**。  
  - 各要素が、その 1 時刻の全測定項目をキーにしたオブジェクト（`station_id`・`observed_datetime` と PM25, OX など）。

時系列グラフには series、地図や表の 1 時刻表示には snapshot が向きます。

## 補足

- `from` と `to` を同じ時刻にすることと `format=snapshot` は別の意味。  
  - 前者は「取得する時間範囲が 1 時刻」という指定。  
  - 後者は「返却形式をスナップショット（局単位配列）にする」という指定。  
- `format=snapshot` のときは仕様上、`from` と `to` を同一時刻にすることが必須。
