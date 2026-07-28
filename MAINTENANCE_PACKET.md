# 保守セッションパケット（自動生成）

> 生成: 2026-07-22 20:14:50 / maintenance_packet.py（手動編集禁止）
> AIエージェントは改修着手前に本パケットを読了すること。
> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md

## ① 監査結果

### ✅ SSOT監査（config_audit）（exit 0）
      ⚠️  stock_analyzer_main_backup.py: AGENTS_DIR
    ============================================================
    ✅ CRITICAL なし

### ✅ パイプライン整合（pipeline_registry_gen --audit）（exit 0）
    パイプライン監査: ステージ105 / 問題0件

### ✅ results契約（results_schema --audit）（exit 0）
    resultsスキーマ監査: 契約142キー / 実コード143キー
      ✅ 契約とコードは完全一致

### ✅ 回帰テスト（pytest tests/）（exit 0）
    ........................................................................ [ 93%]
    ..............................                                           [100%]
    461 passed, 1 skipped in 3.73s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 2026-07-22: 成功53 / 失敗0 / スキップ0
- 👻 記録なし: key_health, screener, excel_export, edinet_rotation, us_sector_bridge, ml_meta

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-07-22] 修正/文書: playbooks/を正本として重複paper/playbooks/を削除。GitHub Actions正本を.github/workflows/daily_run.ymlだけに統一し、EdinetDB同期を含む全処理を09:00 JSTへ変更（週次判定もUTC月曜へ追随）。config.py/Colab時刻=09:00、Obsidian同期=10:30へ連動。EDINETDB_API取得・コール数・運用仕様_20260722.mdを01フォルダへ追加。
- [2026-07-22] 修正: EdinetDB公式更新08:00 JSTより前の06:00日次取得を廃止し、.github/workflows/edinetdb_ingest.ymlで08:30 JST専用同期（financials/segments/text-blocks/disclosures→BQ）へ分離。config.pyは環境変数opt-in、daily_run.ymlはstock_data/insights全体を永続化。allocation_meta.py等7モジュールの出力をconfig.INSIGHT_DIRへ統一し、ローカル更新用フォルダ由来のroot insights分岐を解消。
- [2026-07-22] 修正: 日次EDINET分析の外部REST依存を廃止し、base_agent.py / edinet_bq_client.py / config.py / api_key_health.pyをBQ edinet.*既存行のread-only一括参照へ変更。us_fundamental_rotator.pyはSEC companyfacts 404時にYahoo四半期→年次財務へフォールバックし、stock_analyzer_main.pyはpaper/ml配下のリポジトリ相対解決を追加。重複paper/paper/は完全一致確認後に削除。検証: BAESY年次4期/DTEGY四半期5期の実取得、pytest 455 passed, 1 skipped、config/pipeline/results全監査✅。
- [2026-07-21] 追加: W1/W2を実装。db_rest_client.pyにX-API-Key・日次永続台帳・/usage実残量制御・429/5xx限定再試行を追加し、tests/test_db_rest_client.pyで予算/認証/CI skip実API契約を検証。us_bq_schema.pyのus.fundamentals DDL、us_fundamental_rotator.pyの四半期平坦化と(ticker, period_end) MERGE、config.py BLOCK 60、tests/test_us_fundamentals_bq.pyを追加。pytest 403 passed, 1 skipped・config/pipeline/results監査✅。
- [2026-07-21] 修正: BUG-05（custom_universeのデッドticker掃除）を安全化。dead_ticker_pruner.pyの第2パスはYahoo chart APIの明示HTTP 404だけを削除可、200=生存、429/5xx/通信失敗=unknown（残置）へ変更。第1パス候補が全体の10%超ならYahooの広域レート制限・障害として確認・applyを中止する。実測で429により600候補まで膨張したが、JSONは変更せず誤削除を防止。pytest 404 passed。
- [2026-07-21] 修正: EdinetDBの任意キー未設定（または日次予算到達）がdaily_run.ymlの単独インデックス更新をexit 1にして全日次処理を停止したため、edinetdb_ingestor.pyをfail-soft化。missing key／予算到達／REST失敗はskippedとして既存BQデータで後続分析を継続する。取得→BQ保存→分析の順序を保証するためstock_analyzer_main.pyのsource ingestionをAPI健全性確認直後へ移動し、EdinetDB・FudosanDB・SEC財務をセクター/米国スクリーニングより先に実行、旧位置では二重実行を防止。daily_run.ymlの重複単独ステップを撤去。検証: pytest 401 passed、config/pipeline/results監査✅。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
