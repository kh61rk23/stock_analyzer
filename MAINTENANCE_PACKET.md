# 保守セッションパケット（自動生成）

> 生成: 2026-07-17 00:30:53 / maintenance_packet.py（手動編集禁止）
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
    ........................................................................ [ 80%]
    .......................................................................  [100%]
    359 passed in 0.56s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-07-16] 修正: BQ 403（edinet_rotation 3日連続失敗）の根本原因是正＋pruner v1.2。①edinet_bq_client.py query_to_df: **2026-07-12に導入したgoogle-cloud-bigquery-storageが原因**。同パッケージ導入でto_dataframe()がStorage Read API経路へ自動切替され bigquery.readsessions.create 権限（roles/bigquery.readSessionUser）を新規要求→SAは書込/ジョブ権限のみで403（メール通知3日連続と導入日が一致・エラーメッセージの権限名も一致）。create_bqstorage_client=False でREST経路に固定し権限付与に依存しない構成へ（読取量的にREST十分・IAM付与後にTrueへ戻す選択肢はTODOに記載）。doc_index未更新（テーブル実在は健太がGCPで確認）も同403の巻き添えの可能性が高く、次回ログ「N件挿入」で最終確認。to_dataframeはリポジトリ全域でこの1箇所のみと確認。②dead_ticker_pruner.py v1.2: v1.1でも防げない失敗モードを実測で発見=yfinanceは失敗時に例外でなく空DataFrameを返すためunknownに落ちずdead誤分類（実害: 実行中盤のDNS断でsp500のM以降215銘柄が偽デッド・計369件）。デッド候補を60秒後に再検証する2パス方式＋再検証後もデッド率>50%ならapply拒否を標準化。v1.2で--apply再実行（結果は完了後に本CHANGELOGへ追記）。検証: pytest 335件全通過 / pyflakes新規0 / py_compile OK。
- [2026-07-16] 修正/検証: weekly実行ログ照合#2（ログ_20260713_weekly・修正プッシュ前の実行分）。Fableが5 Issueを設計し、Opus1体+Sonnet3体へ実装委任→全PRをFableがコード実体・独立再実行でレビューし承認（品質不足による上位モデル切替は発生せず）。①I-1[Opus] technology_mapper.py: TECH_KEYWORD_MAPがconfigに未定義（2026-06-10 SSOT再建で消失した幻キー）→technology_mapper→cluster_engine→cluster_graph_exporterの3連鎖が毎週全滅し、taxonomy draft層の生産が止まっていた。承認済みseed 293ノードから動的構築する build_keyword_map_from_seeds＋_get_keyword_map（config優先・無ければseed）を新設、参照3箇所を置換。tests/test_technology_mapper_keywords.py 5件新規。Fable再検証=293ノード・空キーワード0。②I-2[Sonnet] arxiv_monitor/config BLOCK 2: arXiv RSSは「当日分のみの日次ダイジェスト」仕様で低頻度カテゴリ（q-fin.PM等）は正当に0件→日付非依存のarXiv API(export.arxiv.org/api/query)をprimaryに変更（5カテゴリ・実測50件返却・旧RSSはfallback残置）。③I-3[Sonnet] business_summary_fetcher.py: DeepL 403/456をfail-fast化（実行内フラグで以降スキップ・英語原文フォールバック維持）。④I-4[Sonnet] us_fundamental_rotator.py: SEC CIK取得失敗の根本原因2つをcurl実証=(A)連絡先なしUA拒否（UAが有っても403）(B)COMPANY_TICKERS_URLがdata.sec.gov由来で404（正=www.sec.gov）。SEC_USER_AGENT規約に統一・_fetch_submissions/_fetch_xbrl_factsの同型UAも修正・非200診断ログ追加。実取得10,426社を確認。⑤I-4派生[Fable] us_8k_monitor.py: 同型の壊れUA2箇所を_sec_user_agent()に統一。⑥I-5 doc_indexer BQ確認: Chrome拡張未接続のためブラウザ検証は不可→代替として新旧ホストのDNS/TLS疎通をcurl実証（旧=解決不能・新=応答あり）。最終確認は次回Actionsログ「[doc_indexer] 完了: N件挿入」（N>0）で行う。⑦I-6 ❌銘柄8件=EdinetDBレート100回/日到達の仕様内動作と判定（対応不要）。検証: pytest 335件全通過（+5）/ config_audit 0 / pyflakes新規0 / maintenance_packet✅。
- [2026-07-12] 修正/検証: 実行ログ照合#1（logs_78994925401・4ファイルプッシュ前の実行）＋スキーマ照合＋バグ7件修正。①スキーマ検証: bq_all_schemas_20260712.json=25表で**期待どおり完全一致**（20260704比の差分=us4表追加のみ・列レベルもcompany_taxonomy_map16列等コード契約と一致）。SPEC_db_schema.mdへ差分⑧⑨⑩追記＋§10「スキーマ×ジョブ連携」新設（表×producer×タイミング×日次更新量）。②ログ照合の主要成果: dd_controller初回NORMAL✅/score_calculator33銘柄✅/taxonomy_hotness293ノード✅/exit_rule_engine発火1件✅。**paper/とml/がGitHub未プッシュと実証**（「見つかりません」ログ→健太タスク化）。③修正(a) edinet_doc_indexer.py・edinet_direct_fetcher.py: EDINET_BASEが実在しないホストdisclosure.edinet-api.go.jp（DNS解決不能で常時0件）→disclosure.edinet-fsa.go.jpへ是正。(b) gov_data_pipeline.py: e-Stat応答のカテゴリ混在を是正（同一カテゴリ系列のみ採用・建設着工-83.5%等の根本原因）＋鮮度ガード（最新2年以上前は無効化・勤労2009/労働力2005の流入遮断）＋|前年比|>50%異常値クランプ。tests/test_gov_data_quality.py新規5件。(c) economic_cycle_detector.py: GDP N/A根本原因=imf_cache.json実構造（indicators→指標→国）との不整合を是正・CPI N/A根本原因=gov実構造（stat_results配列）との不整合を是正（いずれもproducer/consumerキー不整合）。(d) country_risk_updater.py: GDELT429対策（429即中断リトライなし・累計8失敗でも回路開放・上限30→15国。25分浪費→数分へ）。(e) paid_media_searcher.py: CSE 403/429を3回検出で以降スキップ（12連続403の実害）。(f) viewer/data_bridge.py: streamlit import後にcache_data_apiロガー抑制（No runtime found大量WARNING）。(g) requirements.txt: google-cloud-bigquery-storage追加（UserWarning+低速REST回避）。④dead_ticker_pruner v1.1: **v1.0の全滅誤報告バグ発見**（例外=デッド扱いのため、yfinance未導入環境でAAPL/MSFT含む1460/1463デッドと誤報告）→3値判定（alive/dead/unknown）・起動時環境検査・デッド率>50%でapply拒否のセーフガード実装。Macにyfinance1.5.1導入し再実行（結果待ち→検証後--apply予定）。⑤Excel出力廃止の完遂: daily_run.ymlの.iqy生成を削除＋コミットのCSV存在ゲート撤去（ゲート残置だと全状態ファイルの永続化まで止まる構造欠陥）・ENABLE_EXCEL_EXPORT=False。⑥TODO_LIST: BUG-02修正/BUG-03対応/BUG-04クローズ/BUG-05対応中へ更新＋人間タスク追加（**paper/・ml/のアップロード最重要**・POLICY_FRED_API_KEY登録・GOOGLE_CSE_API_KEY点検）。検証: pytest 330件全通過（+5）/ config_audit 0 / YAML構文OK / maintenance_packet✅ / pyflakes新規0。
- [2026-07-12] 修正/追加/決定: 意思決定一括回答の反映＋Actions永続化の重大修正＋UI説明書。①mermaid構文エラー修正: UML_シーケンス図集の図3（<ticker>がHTMLタグ解釈→除去）と図6-3（日本語状態ID→as別名化）、図5のp<0.10も予防修正。②**daily_run.yml重大修正**: git永続化リストにWave R0〜R5の状態ファイル6点が欠落していた（edinet_retry_queue.json/exit_engine_baseline.json/dd_state.json/paper//cache/taxonomy//data/ml_dataset/）→追加。未修正だとML特徴量・S2口座・DD peak・hotnessベースライン・繰越キューが毎朝リセットされ蓄積が始まらなかった。requirements.txtにPyYAML明示（従来はtransformers経由の間接依存頼み）。setup_us_bq.yml/setup_subtheme_bq.ymlは変更不要と確認。③意思決定の実装: V-01=a（config BLOCK 39をローカルvaultへ是正＋iCloud側Vault2の2箇所に凍結README設置・実パス解決を検証済み）／D-02=b（S8_LLMニュースアルファ週次手動_手順書_20260712.md新規＋data/llm_news_signal.json雛形。signal_aggregator配線は4週蓄積後にD-19起票と明記）／D-03承認（判断台帳へ「S12信用見送り確定」を記録→R5-3は実装対象外）／D-08一括承認（86銘柄）／D-10=a（健太がIBKR開設→完了後R4-3着手）／D-11=a（オプションはカジノ枠内）。④説明書_リスクダッシュボードUI_20260712.md新規（S9の6パネルの見方・halt_flagトグル・トラブルシュート）。⑤MODULE_DISCOVERY_GUIDE.md更新（148→217ファイル・MAINTENANCE_GUIDEへの導線追記・現役継続=bk不要）。⑥健太がHANDOFF4本を01/bk/へ移動→残プッシュ一覧の正本はTODO_LIST §0.5へ一本化（追加プッシュ4ファイルを記載）。検証: pytest 325件全通過・config_audit 0・maintenance_packet総合判定✅。
- [2026-07-12] 設計/棚卸し: 全体棚卸し＋ドキュメント体系の整備（コード変更なし）。①新規: 02_/UML_シーケンス図集_20260712.md（正本宣言・デプロイ図・日次バッチ/B部門審議/MLメタの各シーケンス・戦略ユニット構成図・状態機械3種[dd_controller/playbook/taxonomyノード]・検証コマンドの型。Vault2 departments-Bとの物理パス一覧を明記）／02_/MAINTENANCE_GUIDE.md（「何を変えたい→どこを触る→どう検証」の逆引き10節＋禁止事項）。②更新: 02_/Overhead view_20260712.md 全面更新（100ステージ・23カテゴリ・BQ25表・R0〜R5-2Aレイヤ反映。旧20260704版は02_/bk/へ退避）／SPEC_investment_dept.md（vault二重の実態・R3-2/二層運用/exporter v2を追記）／TODO_LIST.md §0.2新設（残意思決定の正本表: V-01/D-02/D-03/D-08/D-10/D-11/D-17/D-18＋決定済み一覧＋将来の承認ゲート）。③発見（重要）: vault書き先分裂リスク=V-01。実働正本はローカル側Vault2_ClaudeCode_Localだが config BLOCK 39 既定値は2026-06-12凍結のiCloud側を指す→V-01決定までexporter/scheduler/obsidian_syncのデフォルト実行禁止。obsidian_syncはlaunchdロード済みだが両vaultに05_最新データが不存在=実効未検証。launchd invest-weeklyは未ロード（plistのみ）。④確認: D-09はNotion廃止で決定済み（config L4304-4306に記録・memory側の旧TODO「NOTION_API_KEY登録」は失効）。
- [2026-07-12] 追加/設計: Wave R4-4・R5-1・R5-2 Phase A 実装（R4-3/R5-3/R5-4は要件どおり保留）。①R4-4(S11マルチ戦略資金配分メタ): allocation_meta.py 完成（前セッション書きかけを仕上げ: build_report L215の未解決プレースホルダ{getattr}バグ修正→plan["min_months"]経由に是正）＋tests/test_allocation_meta.py 新規17件（確定式=分数ケリー0.25×リスクパリティ・カジノ枠制約充足・PF<1.0かつp>0.5淘汰・データ不足保留・レポート回帰）。config BLOCK 50は既存。main配線なし（R4-2と同じ月次スタンドアロン: python3 allocation_meta.py YYYYMM）。②R5-1(S7 MLメタモデル): ml/ 新規パッケージ=dataset_builder.py（日次で23カテゴリ+レジーム4象限+size_tier+業種を features.jsonl へ冪等追記・targetはColabで後付け）・purged_cv.py（Purged Walk-Forward CV embargo20営業日・rank IC・t統計＝純Python・Colabと共通中核）・corrector_applier.py（補正テーブル適用時にも昇格基準IC>=0.02かつt>=2.0を再検証・decile→composite±10pt上限・ML_META_ENABLED=True時のみ）・train_meta.ipynb（Colab週次LightGBM学習→昇格合格時のみJSONエクスポート）。config BLOCK 51へ ML_META_N_DECILES/CV_FOLDS/DATASET_PATH/CORRECTION_PATH/DATASET_MAX_DAYS/ENABLE_ML_META_DATASET 追記＋DAILY_MODULESに ml_meta 追加。mainに2ステージ追加（snapshot=常時軽量・corrector=フラグ時のみ）→results_schema に ml_meta_dataset/ml_meta_correction。tests/test_ml_meta.py 新規22件（受入条件=リーク日付シフトテスト・embargo有無で分割が変わる/削減幅=horizon+embargo=40日・末尾H日は目的変数なし・snapshot冪等・±10pt超テーブル適用拒否）。③R5-2 Phase A(E10 US同等化): score_calculator.py 新規（SPEC_core §8-2）＋config BLOCK 56（ENABLE_SCORE_CALCULATOR/UNIFIED_SCORE_US_TOP_N=50）＋DAILY_MODULESに score_calculator。adapterは既存スコアの写像のみ（JP=screener候補→sectors業種フォールバック・US=us_screener）で再計算なし=SSOT分裂回避。対象=保有＋US上位50。JP負域-30..0は0..23へ線形正規化。results_schema に unified_scores。tests/test_score_calculator.py 新規13件。SPEC_core §8-3にPhase A設計判断を追記。検証: pytest 325件全通過（+52）/ config_audit 0 / registry 100ステージ・監査問題0 / results_schema 完全一致(139キー) / pyflakes新規0件。未検証（人間）: 次回Actionsログ「[ml_meta_dataset]」「[score_calculator]」/ features.jsonl 蓄積確認（学習は90日以上蓄積後にColabで）/ allocation_meta はS2成績3ヶ月蓄積後に月次実行。保留: R4-3(S4オプション)=D-10 IBKR口座開設待ち・R5-3(S12信用)=D-03決議まで着手禁止・R5-4(S8)=D-02まで凍結。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
