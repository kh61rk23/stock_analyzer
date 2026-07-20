# 保守セッションパケット（自動生成）

> 生成: 2026-07-19 09:48:05 / maintenance_packet.py（手動編集禁止）
> AIエージェントは改修着手前に本パケットを読了すること。
> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md

## ① 監査結果

### ✅ SSOT監査（config_audit）（exit 0）
    [D] 存在しない cfg 属性参照（WARNING）: 0ファイル
    ============================================================
    ✅ CRITICAL なし

### ✅ パイプライン整合（pipeline_registry_gen --audit）（exit 0）
    パイプライン監査: ステージ101 / 問題0件

### ✅ results契約（results_schema --audit）（exit 0）
    resultsスキーマ監査: 契約139キー / 実コード140キー
      ✅ 契約とコードは完全一致

### ✅ 回帰テスト（pytest tests/）（exit 0）
    ........................................................................ [ 93%]
    ........................                                                 [100%]
    384 passed in 0.54s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-07-19] 修正/追加: 健太報告の実証①〜⑧を一括対応（Fable・BQ/GitHub両コネクタの実データで裏取り）。①api_key_health.py _probe_bq: EDINETBQClientはquery()を持たないラッパーのためAttributeError→「SELECT 1失敗（認証・権限エラーの疑い）」と誤報告していた（同一実行内でBQ MERGE 59件成功＝**IAMロールは正常・GCP追加作業不要**）。生クライアント(.client)でSELECT 1する方式へ是正＋例外型名をログ出力。②country_risk_updater.py: travel.state.gov 403（Actions IP恒常ブロック）を非リトライ化し、RSS取得失敗時は合成スコアを残ソースで再正規化（mofa_available/state_available新設。「取得失敗」を「レベル0=安全」と誤解釈しない）。③同: GDELT Doc API恒常429の代替として**BigQuery公開データセット経路をprimary実装**（gdelt-bq.gdeltv2.events_partitioned・紛争系EventRootCode 14/15/17/18/19/20の平均トーン・_ISO3_TO_FIPS 59国マップ・1クエリ数MBスキャン）。59/59国の実データ取得をMCP直queryで検証済み。失敗時のみDoc API→減衰キャッシュへフォールバック。④segment_signal.py+edinet_bq_client.py: 幻のAPI（bq.get_segment_data/bq.query）と実在しないテーブル（taxonomy.segments_master/edinet.segment_financials）参照で常に0件だった読取経路を、get_segment_finance実装（edinet.segments×segment_finance実結合）＋build_segment_view（最新期抽出・growth_rate算出の純関数）で是正。⑤edinet_direct_fetcher.py: (a)EDINET API v2のSubscription-Keyヘッダ欠落（キーがあっても401になる構造）(b)financials/segments書込行が実スキーマと全列不一致（fin_id等REQUIRED欠落）を是正し segments+segment_finance の2表書込へ（build_financials_row/build_segment_rows純関数化）→**EDINET_API_KEY再発行(⑧)後に蓄積が実際に始まる前提条件を整備**。⑥text_extractor.py: EDINETDB_API_KEYで誤ってAPIルートを起動し毎銘柄2回無駄打ちしていたゲートをEDINET_API_KEY専用へ＋キー未設定時はバッチ冒頭1行明示。⑦GitHub SHA照合: 07-19朝の健太アップロードで要プッシュ分はほぼ同期済みと確定（残=backtest/一式のみ・paper/paper/重複ネスト残骸。今朝の実行のみ旧yml定義で発火しfatalノイズが出た＝次回から解消）。tests/test_country_risk_bq_route.py＋tests/test_segment_pipeline.py 25件新規。検証: pytest 384件全通過／pyflakes新規0／TODO_LISTへ人間側コマンドのフルパス化と07-19セクション追加。
- [2026-07-16] 修正: GITHUB_SECRETS_LEDGER.md のAPIキー平文5件を健太指示で復旧（前エントリでAIが削除したもの。台帳に値も記録する運用を健太が選択・記録節に留意1行残置）。TODO_LISTのセキュリティ項目をクローズ。
- [2026-07-16] 修正/追加/セキュリティ: daily照合#3＋BQ実データ検証＋APIキー健全性チェック実装（Fable設計・Opus/Sonnet実装・Fableレビュー）。①**セキュリティ**: GITHUB_SECRETS_LEDGER.md にAPIキー平文5件が記入されていたため即削除（露出扱いの判断は健太へ・TODO_LIST起票）。②**BQ実データ検証（MCP直query）**: financials/segments/text_analysis/taxonomy_node/company_taxonomy_map=全て0行と確定→segment_signal全業種0pt・text_extractor全滅は**コードバグでなく上流データ空**（EDINET蓄積ゼロ）が原因。doc_index=193行だが全行fetched=true・disclosure_date NULLの旧データで実質未更新。country_risk_daily=1062行は正常稼働。③**「状態ファイルがGitHubに生成されない」の根本原因特定**: commitステップで「64 files changed」成功後に pull --rebase が「You have unstaged changes」で失敗→リモート先行日はpushが||trueで黙殺され**全状態ファイルがロスト**していた（実行ログで実証）。daily_run.ymlを pull --rebase --autostash 化＋push成否を明示ログ化＋git addのfatalノイズ64行を2>/dev/null抑制。④J-1[Sonnet] country_risk_updater.py GDELT v2: 最終取得値の永続キャッシュ（stock_data/cache/gdelt_instability_cache.json・yml永続化追加）→取得失敗時は日次5%減衰で代用（♻️ログ）・回路閾値5→3・JSONパース失敗も失敗カウント。tests/test_gdelt_cache.py 11件。Fable検証=減衰42.0/6日・下限0。⑤J-2[Opus] **api_key_health.py新規（506行）**: 健太要望「キー生死をログで判定」。EDINET/EdinetDB/e-Stat/FRED/DeepL/CSE(既定OFF)/Gmail SMTP/BQ を軽量1コールで✅❌⚠️⏭️判定し実行ログ冒頭に「🔑 [key_health]」サマリ＋詳細行を出力。probe部と判定部を分離した純関数設計・キー値のマスキング検査つき・config BLOCK 57・main序盤ステージ・registry 101ステージ化。tests 13件。Fable裁定3件を反映=(a)EdinetDBは認証不要searchだと偽✅→認証必須analysisへ変更（予算1コール許容） (b)EDINETキーはURLパラメータでなくヘッダOcp-Apim-Subscription-Key（既存流儀）に統一 (c)BQはEDINETBQClientクラスで固定。⑥dead_ticker_pruner v1.3: 第1パスをyf.downloadバッチ化（100銘柄/リクエスト・リクエスト数1/100）＝v1.2実行で642件が偽デッドのままガード発動した「レート制限の自己誘発」への根本対策。applyは翌日以降に健太実行。⑦GITHUB_SECRETS_LEDGER.md: 平文削除＋是正記録＋EDINETDB再発行✅/EDINET失効疑いを記録。検証: pytest 359件全通過（+24）/ config_audit 0 / registry 101・問題0 / results_schema 完全一致 / maintenance_packet✅。
- [2026-07-16] 修正: BQ 403（edinet_rotation 3日連続失敗）の根本原因是正＋pruner v1.2。①edinet_bq_client.py query_to_df: **2026-07-12に導入したgoogle-cloud-bigquery-storageが原因**。同パッケージ導入でto_dataframe()がStorage Read API経路へ自動切替され bigquery.readsessions.create 権限（roles/bigquery.readSessionUser）を新規要求→SAは書込/ジョブ権限のみで403（メール通知3日連続と導入日が一致・エラーメッセージの権限名も一致）。create_bqstorage_client=False でREST経路に固定し権限付与に依存しない構成へ（読取量的にREST十分・IAM付与後にTrueへ戻す選択肢はTODOに記載）。doc_index未更新（テーブル実在は健太がGCPで確認）も同403の巻き添えの可能性が高く、次回ログ「N件挿入」で最終確認。to_dataframeはリポジトリ全域でこの1箇所のみと確認。②dead_ticker_pruner.py v1.2: v1.1でも防げない失敗モードを実測で発見=yfinanceは失敗時に例外でなく空DataFrameを返すためunknownに落ちずdead誤分類（実害: 実行中盤のDNS断でsp500のM以降215銘柄が偽デッド・計369件）。デッド候補を60秒後に再検証する2パス方式＋再検証後もデッド率>50%ならapply拒否を標準化。v1.2で--apply再実行（結果は完了後に本CHANGELOGへ追記）。検証: pytest 335件全通過 / pyflakes新規0 / py_compile OK。
- [2026-07-16] 修正/検証: weekly実行ログ照合#2（ログ_20260713_weekly・修正プッシュ前の実行分）。Fableが5 Issueを設計し、Opus1体+Sonnet3体へ実装委任→全PRをFableがコード実体・独立再実行でレビューし承認（品質不足による上位モデル切替は発生せず）。①I-1[Opus] technology_mapper.py: TECH_KEYWORD_MAPがconfigに未定義（2026-06-10 SSOT再建で消失した幻キー）→technology_mapper→cluster_engine→cluster_graph_exporterの3連鎖が毎週全滅し、taxonomy draft層の生産が止まっていた。承認済みseed 293ノードから動的構築する build_keyword_map_from_seeds＋_get_keyword_map（config優先・無ければseed）を新設、参照3箇所を置換。tests/test_technology_mapper_keywords.py 5件新規。Fable再検証=293ノード・空キーワード0。②I-2[Sonnet] arxiv_monitor/config BLOCK 2: arXiv RSSは「当日分のみの日次ダイジェスト」仕様で低頻度カテゴリ（q-fin.PM等）は正当に0件→日付非依存のarXiv API(export.arxiv.org/api/query)をprimaryに変更（5カテゴリ・実測50件返却・旧RSSはfallback残置）。③I-3[Sonnet] business_summary_fetcher.py: DeepL 403/456をfail-fast化（実行内フラグで以降スキップ・英語原文フォールバック維持）。④I-4[Sonnet] us_fundamental_rotator.py: SEC CIK取得失敗の根本原因2つをcurl実証=(A)連絡先なしUA拒否（UAが有っても403）(B)COMPANY_TICKERS_URLがdata.sec.gov由来で404（正=www.sec.gov）。SEC_USER_AGENT規約に統一・_fetch_submissions/_fetch_xbrl_factsの同型UAも修正・非200診断ログ追加。実取得10,426社を確認。⑤I-4派生[Fable] us_8k_monitor.py: 同型の壊れUA2箇所を_sec_user_agent()に統一。⑥I-5 doc_indexer BQ確認: Chrome拡張未接続のためブラウザ検証は不可→代替として新旧ホストのDNS/TLS疎通をcurl実証（旧=解決不能・新=応答あり）。最終確認は次回Actionsログ「[doc_indexer] 完了: N件挿入」（N>0）で行う。⑦I-6 ❌銘柄8件=EdinetDBレート100回/日到達の仕様内動作と判定（対応不要）。検証: pytest 335件全通過（+5）/ config_audit 0 / pyflakes新規0 / maintenance_packet✅。
- [2026-07-12] 修正/検証: 実行ログ照合#1（logs_78994925401・4ファイルプッシュ前の実行）＋スキーマ照合＋バグ7件修正。①スキーマ検証: bq_all_schemas_20260712.json=25表で**期待どおり完全一致**（20260704比の差分=us4表追加のみ・列レベルもcompany_taxonomy_map16列等コード契約と一致）。SPEC_db_schema.mdへ差分⑧⑨⑩追記＋§10「スキーマ×ジョブ連携」新設（表×producer×タイミング×日次更新量）。②ログ照合の主要成果: dd_controller初回NORMAL✅/score_calculator33銘柄✅/taxonomy_hotness293ノード✅/exit_rule_engine発火1件✅。**paper/とml/がGitHub未プッシュと実証**（「見つかりません」ログ→健太タスク化）。③修正(a) edinet_doc_indexer.py・edinet_direct_fetcher.py: EDINET_BASEが実在しないホストdisclosure.edinet-api.go.jp（DNS解決不能で常時0件）→disclosure.edinet-fsa.go.jpへ是正。(b) gov_data_pipeline.py: e-Stat応答のカテゴリ混在を是正（同一カテゴリ系列のみ採用・建設着工-83.5%等の根本原因）＋鮮度ガード（最新2年以上前は無効化・勤労2009/労働力2005の流入遮断）＋|前年比|>50%異常値クランプ。tests/test_gov_data_quality.py新規5件。(c) economic_cycle_detector.py: GDP N/A根本原因=imf_cache.json実構造（indicators→指標→国）との不整合を是正・CPI N/A根本原因=gov実構造（stat_results配列）との不整合を是正（いずれもproducer/consumerキー不整合）。(d) country_risk_updater.py: GDELT429対策（429即中断リトライなし・累計8失敗でも回路開放・上限30→15国。25分浪費→数分へ）。(e) paid_media_searcher.py: CSE 403/429を3回検出で以降スキップ（12連続403の実害）。(f) viewer/data_bridge.py: streamlit import後にcache_data_apiロガー抑制（No runtime found大量WARNING）。(g) requirements.txt: google-cloud-bigquery-storage追加（UserWarning+低速REST回避）。④dead_ticker_pruner v1.1: **v1.0の全滅誤報告バグ発見**（例外=デッド扱いのため、yfinance未導入環境でAAPL/MSFT含む1460/1463デッドと誤報告）→3値判定（alive/dead/unknown）・起動時環境検査・デッド率>50%でapply拒否のセーフガード実装。Macにyfinance1.5.1導入し再実行（結果待ち→検証後--apply予定）。⑤Excel出力廃止の完遂: daily_run.ymlの.iqy生成を削除＋コミットのCSV存在ゲート撤去（ゲート残置だと全状態ファイルの永続化まで止まる構造欠陥）・ENABLE_EXCEL_EXPORT=False。⑥TODO_LIST: BUG-02修正/BUG-03対応/BUG-04クローズ/BUG-05対応中へ更新＋人間タスク追加（**paper/・ml/のアップロード最重要**・POLICY_FRED_API_KEY登録・GOOGLE_CSE_API_KEY点検）。検証: pytest 330件全通過（+5）/ config_audit 0 / YAML構文OK / maintenance_packet✅ / pyflakes新規0。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
