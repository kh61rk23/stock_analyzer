# stock_analyzer — AIエージェント保守手順書（AGENT-1）

このリポジトリで Claude Code 等のAIエージェントが作業する際の標準手順。
設計の正本は `../統合ファイルなど/02_ファイル構成/`
（SPEC_core.md / SPEC_db_schema.md / SPEC_investment_dept.md / MODULE_INDEX.md）。
進捗管理・引き継ぎ資料は `../統合ファイルなど/01_最新版ロードマップ・必要書類/`
（TODO_LIST.md / SESSION_PACKET.md 等。CHANGELOG.md は2026-09-26に凍結し、変更履歴は board の開発ログ台帳へ移設）。

## 絶対禁止

- **`../stock_analyzer-main_bk` を変更しない**（バックアップ。読むのは可）
- APIキー・トークンのコード内ハードコード（`os.getenv()` + GitHub Secrets 経由のみ）
※資料に記載は可とする。
- config.py のモジュールレベル重複定義（Pythonは後勝ち上書きで黙って壊れる。
  2026-06 に LEADING_INDICATOR_RULES 三重定義で先行指標7本が無効化されていた実害事例）

## セッション開始時（改修着手前に必ず）

```bash
python3 maintenance_packet.py   # 全監査+台帳+直近変更を MAINTENANCE_PACKET.md に集約
```
総合判定が ❌ なら、依頼内容より先に監査NGの修復を提案すること。

## コメントの書き方（2026-10-07 健太方針）

- コードのコメントには「今どう動くか」と「なぜそうしているか（知らずに直すと壊れる理由）」だけを書く。
- 日付・「旧実装は〜だった」・バッチ/Wave番号・誰の指示か、といった**経緯は書かない**。経緯は
  開発ログ（`devlog.py add`）とコミットメッセージに書く。過去分は G-dev `41_stock_analyzer_コード内修正履歴.md` に移設済み。
- コメントだけを直したときは `python3 tools/history_comments.py same-code <rev>` でコード（AST）が変わっていないことを確かめる。

## 役割分担（2026-10-08 健太方針）

複数タスクにまたがる開発は役割を分け、上位モデルから下位へ移譲する。
- 構想（仕様・判断）: Opus 5.5 ／ 実装: Sonnet 5.5
- レビュー: 機械的検査＝Haiku 5.5（テスト・監査・構文・AST比較）→ 論理レビュー＝Sonnet 5.5（仕様との突き合わせ・中身の誤り）
- 最終確認: Opus 5.5（実データでの確認、スコア・取り込み・外部API規約に関わる判断）
- Haiku は量が多く判断の軽い作業にも使う: Actions ログの一次仕分け、広い grep、定型の文書更新（MODULE_INDEX・開発ログ・案件台帳）。
- Workflow では agent の model に 'sonnet' / 'haiku' を指定し、1エージェントの担当は小さく切る（長いと止まる）。

## 改修ループ（診断 → 修正 → 監査 → ドキュメント同期）

| 変更対象 | 変更後に必ず実行 |
|---|---|
| config.py | `python3 config_audit.py`（CRITICAL=exit 1） |
| stock_analyzer_main.py | `python3 pipeline_registry_gen.py`（registry再生成）→ `--audit` |
| results["新キー"] の追加 | results_schema.py の SCHEMA に追記 → `python3 results_schema.py --audit` |
| 純粋ロジック（計算・分類・パース） | tests/ にテスト追加 → `python3 -m pytest tests/ -q` |
| 任意の .py | `python3 -m py_compile <file>` + pyflakes（未定義名0を維持） |

完了時:
1. `python3 -m pytest tests/ -q` 全通過を確認
2. 開発ログ台帳に記録（2026-09-26〜。CHANGELOG.md は凍結）:
   `python3 ~/Documents/Obsidian_Local/Vault2_ClaudeCode_Local/board/devlog.py add --project stock_analyzer --kind 修正 --title "…" --detail "…" --files a.py --commit abc1234`
   （board「システム › 開発」/dev で全プロジェクト横断に見られる）
3. **構成管理資料を必ず更新する**（2026-10-03 健太指示：改修のたびに更新）
   - `02_ファイル構成/MODULE_INDEX.md`：追加・変更したモジュールとワークフローの行
   - `02_ファイル構成/SPEC_db_schema.md`：テーブル・列・書き手・実測行数（§0-1）を変えたら必ず
   - `02_ファイル構成/CONFIG_REFERENCE.md`：`python3 config_reference_gen.py` で再生成（手編集しない）
   - `08_jsonファイル,テーブル定義/bq_all_schemas_<日付>.json`：BQの表・列を変えたら取り直す
   - SPEC_core.md：処理の流れ・構造を変えたら
4. **開発ダッシュボードを更新する**：board の G-dev `10_案件台帳.md` の stock_analyzer 行
   （概要・現在地／次アクション）を最新にする（開発ログ＝/dev の明細と、台帳＝現在地の両方）

## このシステムの構造（30秒）

- エントリ: GitHub Actions（cron "17 0 * * 1-6" = 月〜土 09:17 JST / 月曜のみ自動でweeklyモード）
  → run_headless.py → stock_analyzer_main.run_all_tasks()
  ※ 日曜は市場が動かないため停止（Actions 無料枠 2,000分/月の節約）。
    UTC 00:00 ちょうどはジョブが集中し実測で3〜4時間の遅延が出るため :17 へずらしてある。
    **前倒し（UTC 23:40 等）は不可** — JST月曜の回が UTC では日曜になり weekly 判定が壊れる。
- 設定SSOT: config.py（BLOCK 0〜39・他moduleをimportしない）
- パイプライン宣言: pipeline_registry.json（108ステージ・AST自動生成・手動編集禁止）
- データ契約: results_schema.py（145キー）
- 観測性: module_ledger.py（実行台帳・連続失敗アラート・配線漏れ検出）
- 投資部連携: agent_packet_exporter.py → Obsidian vault（B-investment）
- ローカル同期: obsidian_sync.py（GitHub→Mac→iCloud vault / launchd は setup/ 参照）

## 過去の重大バグのパターン（再発検知の勘所）

1. **サイレント失敗**: try/except が握り潰す → 実行台帳の「記録なし」「連続失敗」を見る
2. **幻のAPI**: 呼び出し側が期待する関数が未実装（ImportError/AttributeError が握り潰される）
   → 動的ロード属性契約の検査（pipeline監査が検出）
3. **producer/consumerキー不整合**: results の書き手と読み手でキー名が違う
   → results_schema --audit が検出
4. **到達不能コード**: return の後ろに処理が残る → AST検査
5. **mode名をモジュールキーとして渡す**: `_in_mode("weekly")` はモード名であり
   DAILY/WEEKLY_MODULES のどちらにも無いため**常に False**。そのブロックは
   一度も実行されない（2026-09 に求人票/役員転職/施設着工の3件が該当し、
   対応する BQ テーブルが作成以来ずっと0行だった）
   → `tests/test_bq_wiring.py` が機械検査する。2026-10 に `_in_mode("all")` でも同じ事故
     （13F が導入以来未実行・us.institutional_positions 0行）。現在は「_in_mode に渡すキーは
     DAILY/WEEKLY_MODULES に登録済みか、廃止済みリストにあること」まで検査している
6. **外部APIの間隔なし連打**: 送信間隔を空けずGETを連発すると429を誘発し、
   その再試行が日次予算を食い潰して全件失敗する。さらに予算切れパスで
   `last_error` を立て忘れると呼び出し側の打ち切りが発火せず空振り周回する
   （2026-09 に edinetdb 0/11・fudosandb 0/45 の実害）
7. **yfinance の MultiIndex 逆参照**: `group_by="ticker"` の DataFrame は
   level0=ticker。`data["Close"][ticker]` は KeyError になり except で
   全銘柄が黙って捨てられる。正しくは `data[ticker]["Close"]`
8. **インデント1段による自己上書き**: `else:` を伴わない後始末行が
   成功側ブロックの末尾に紛れ込み、直前に入れた値を毎回潰す。
   集計値（signal_score 等）は先に算出済みのため**表向きは正常に見え**、
   その内訳を読む側だけが全滅する。2026-09 に signal_aggregator の
   `components[cat] = None` が該当し、初版から5ヶ月半のあいだ
   ml_meta が0行・地政学アラートが未発火だった
   → 「集計値は出ているのに内訳が空」を疑う。
     `tests/test_signal_components_survive.py` が機械検査する
9. **監視側が実態からずれて常時アラート**: 鮮度・SLA・死活の監視が
   産出側のパス／キー／更新周期とずれると、毎日アラートが出続けて
   誰も見なくなる（オオカミ少年化）。2026-09 に cache_freshness が
   71日連続 alert（パス3件・時刻キー1件・半減期1件のズレ）
   → 監視項目を足すときは**産出側の実ファイルを1度は開いて確認**する。
     「未生成」と「陳腐化」は別状態として扱うこと
10. **文字列で組み立てたSQLの構文エラー＋失敗の格下げ**: f文字列でMERGE文を組む箇所で
   `STRUCT<` の `<` が抜け（JSON列には文字列リテラルを渡し）、全件が構文エラーだった。
   しかも呼び出し側が「書けなかった」を warn 扱いにして終了コード0を返し、月次台帳は毎月
   success。geopolitics.company_country_exposure / country_profile が導入以来0行（2026-10-03 発覚）
   → SQL生成関数を足したら **BQ MCP の dryRun で構文と型を一度通す**。
     「抽出できたのに書けなかった」は失敗として返す

## データが入らない系の調査手順（BQ実データ起点）

「DBに入るはずのデータが入っていない」を疑うときの定石。
詳細な手順と判断基準は skill `stock-analyzer-refit` に記載。

1. **BQの実測から入る**（推測から入らない）。MCPコネクタで各データセットの
   `__TABLES__` を引き、行数と最終更新日を一覧化する。メタデータ参照なので
   `totalBytesBilled=0`＝**課金されない**。
   ```sql
   SELECT table_id, row_count, DATE(TIMESTAMP_MILLIS(last_modified_time))
   FROM `<project>.<dataset>.__TABLES__` ORDER BY row_count
   ```
   ※ プロジェクトは `stock-analyzer-edinet`。MCPのクエリでは
     テーブル参照のバッククォートを外す（付けると引数が届かないことがある）
2. 0行テーブルごとに、コード側に**書き込み処理があるか**を grep で分類する。
   - 書き込み処理あり → **配線漏れ**（要修正）
   - `edinet_bq_schema.py` にDDLがあるだけ → 予約テーブル（対応不要）
   - `_p1` など後継テーブルが稼働中 → 旧テーブル（対応不要）
3. 配線漏れは producer 側を疑う。**consumer だけ実装済み**というパターンが多い
   （例: `base_agent.get_policy_support_bonus()` は配線済みなのに
   `run_nedo_selection_scraper()` がどこからも呼ばれていなかった）
4. **出力ファイルと永続化データの項目差分**を突き合わせる。md/json に出ている
   のにDBにもJSONにも残らない項目は、その日を過ぎると永久に失われる
   （2026-09 の銘柄md調査では14項目が該当。DB移管提案の決め手になった）

## 課金ガード（Google Cloud）

**大原則: BigQuery の費用は「データ量」ではなく「クエリ本数」で決まる。**
BQ は**参照テーブル1つにつき最低10MB**を課金する。本システムの全テーブル
合計は約69MB（2026-09-10 実測・最大は edinet.text_analysis の 54.5MB）で
無料枠 1TB/月 に対し十分小さいため、スキャン量が問題になることはまず無い。
効くのは本数だけである。

実測での確認方法（**INFORMATION_SCHEMA は課金されない**）:

```sql
-- 日別の課金バイトとクエリ本数
SELECT DATE(creation_time) d, COUNT(*) jobs,
       ROUND(SUM(total_bytes_billed)/POW(1024,3), 3) gb_billed,
       ROUND(SUM(total_bytes_processed)/POW(1024,3), 3) gb_processed
FROM `region-asia-northeast1`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  AND job_type = 'QUERY'
GROUP BY d ORDER BY d DESC
```

`gb_billed` が `gb_processed` を大きく上回っていたら10MB下限が支配的、
すなわち**N+1が残っている**サイン（2026-09-10 時点で 6.9GB vs 3.0GB だった）。
本数の内訳は同じテーブルを `GROUP BY query の先頭130文字` で集計する。
※ `ontology` と `us` データセットは location=US のため
  `region-us`.INFORMATION_SCHEMA を別途見る。

- `BQ_MAX_BYTES_BILLED`（既定10GB/クエリ）が全クエリに効く。
  これは**事故を止めるだけ**で、本数由来の費用には効かない
- **ループ内でBQを引かない**。銘柄・業種ごとに引くと本数がそのまま費用になる。
  集約済みの入口（2026-09）:
  - `get_tickers_by_sector_bulk()` — 業種ticker 33本 → 1本
  - `prefetch_real_estate_holdings()` — 保有不動産 85本 → 1本
  - `prefetch_latest_financials()` — 最新期財務 85本 → 1本
  - `prefetch_company_analysis_bundles()` — 業種分析バンドル 33本 → 1本
  - `geo_signal.prefetch_exposures()` — 権益 85本 → 1本
  先読みの起点は `base_agent.resolve_sector_tickers()` の業種ticker一括解決
  直後。1実行で触る銘柄が全部確定する唯一の場所なので、銘柄別のBQ参照を
  足すときはここに先読みを追加する。**該当行が無い銘柄も空で埋めること**
  （埋めないとキャッシュミスで個別クエリに逆戻りする）
- Google CSE: 無料枠100件/日。超過分は課金されるため
  `B5_PAID_MEDIA_SEARCHER_CSE_CALL_LIMIT`（既定90）で打ち切る
