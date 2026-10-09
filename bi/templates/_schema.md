# BIテンプレートの書き方

正本の設計は board `説明書/提案_BI基盤_20261009.md` §2。ここは書き手向けの要約。
検査は `python3 -m pytest tests/test_bi_templates.py`（部品は `bi_templates.py`）。

## 1テンプレの形（`bi/templates/<領域>.json` は配列）

```json
{"id": "fin_company_10y", "domain": "finance", "title": "会社の10年推移",
 "question": "ユーザーが聞きたいこと", "mart": "m_fin_annual",
 "sql": "SELECT ... FROM m_fin_annual WHERE sec_code4 = {{sec_code4}}",
 "params": [{"name": "sec_code4", "label": "証券コード（4桁）", "type": "string", "default": "7203"}],
 "chart": {"type": "line", "x": "fiscal_year", "y": ["net_sales"], "series": null},
 "note": "読み方の注意"}
```

- `id` は英小文字・数字・`_`。全テンプレで一意。
- `chart.type` は `bar | line | scatter | table`。`x`/`y`/`series` は **SQLの結果の列名**（`y` は列名または列名の配列、`series` は無ければ null）。
- `params[].type` は `string | int | number`。`int`/`number` は任意で `min`/`max`。SQLに出る `{{name}}` と宣言は過不足なく一致させる。

## SQLの方言ルール（DuckDB と BigQuery の両方で動く）

- 参照する表は**マート名だけ**（`bi/marts/*.sql` のファイル名。例 `m_company`・`m_country_risk`・`m_re_area`）。完全名・バッククォートは使わない。
  board は DuckDB にマートの parquet を同名のビューとして登録する。Claudeダッシュボードは、bi_export が BigQuery に実体化した
  `stock-analyzer-edinet.bi.<マート名>` を読む（未実体化のマートだけ `bi_templates.compose_bigquery_sql` で
  `WITH <マート名> AS (<bi/marts/*.sql>)` を前置して投げる。元表を毎回スキャンしないので読み取りが小さい）。
- 使える関数（許可リスト）：`SUM AVG MIN MAX COUNT COALESCE NULLIF ROUND ABS ROW_NUMBER RANK DENSE_RANK LAG LEAD`、
  `CASE WHEN`、`OVER (PARTITION BY ... ORDER BY ...)`。
- 禁止：`MEDIAN`・`QUALIFY`・`SAFE_DIVIDE`・`APPROX_*`・配列系（`UNNEST` 等）・`CAST(... AS DOUBLE)`・`FLOAT64`。
  小数化は `x * 1.0`、割り算は `x / NULLIF(y, 0)`。
- 割合は小数のまま保存されている（0.12＝12%）。表示用に `* 100` して `_pct` と名付ける。
- 年の比較は `LAG` で行を引くだけにせず、`LAG(fiscal_year, n) = fiscal_year - n` で「本当にn年前か」を確かめる（欠けた年を跨がない）。
- 外れ値（ROE ±100% 超など）は `BETWEEN` で除く。除いたことは `note` に書く。
- 文字列パラメータは英数字・日本語・記号の一部のみ。`'` `"` `` ` `` `\` `;` `--` `/*` と制御文字は置換時に拒否される。

## 追加の手順

1. `bi/templates/*.json` に追記し、`python3 -m pytest tests/test_bi_templates.py -q` を通す。
2. 新しい列が要るなら `bi/marts/*.sql` を直し、`tests/test_bi_templates.py` の合成データ（列定義）も合わせる。
3. `bi_export.py` が日次で `stock_data/bi/templates/` にコピーする（検査NGがあると終了コード1）。
