# 保守セッションパケット（自動生成）

> 生成: 2026-06-13 08:49:30 / maintenance_packet.py（手動編集禁止）
> AIエージェントは改修着手前に本パケットを読了すること。
> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md

## ① 監査結果

### ✅ SSOT監査（config_audit）（exit 0）
    [D] 存在しない cfg 属性参照（WARNING）: 0ファイル
    ============================================================
    ✅ CRITICAL なし

### ✅ パイプライン整合（pipeline_registry_gen --audit）（exit 0）
    パイプライン監査: ステージ92 / 問題1件
      ⚠️  無効化済みステージのモジュール未配置（許容）: us_macro_pipeline.py (L2885)

### ✅ results契約（results_schema --audit）（exit 0）
    resultsスキーマ監査: 契約130キー / 実コード131キー
      ✅ 契約とコードは完全一致

### ✅ 回帰テスト（pytest tests/）（exit 0）
    .......................................................................  [100%]
    71 passed in 0.11s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-06-12] 追加: ONT-1本番配線 — ontology_exporter.py に run_ontology_exporter() を追加し stock_analyzer_main へ週次配線（WEEKLY_MODULES "ontology_export" / config BLOCK 39b）。BQ認証なし環境はJSONL自動フォールバック
- [2026-06-12] 追加: devil_calibration.py 新規作成（DEPT-3 / 判断台帳成績・conviction別勝率・devil判定A/B/C分布→25_デビル校正ノート.md を自動生成。B3部則とCLAUDE.mdに必読配線。tests 4件追加）
- [2026-06-12] 追加: maintenance_packet.py + リポジトリ直下CLAUDE.md 新規作成（AGENT-1 / 全監査+台帳+CHANGELOG を MAINTENANCE_PACKET.md に集約。AIセッションの標準保守手順を明文化）
- [2026-06-12] 追加: setup/ 新規作成（launchd plist + install_launchd.sh — obsidian_sync 毎朝9時実行のワンコマンド登録）
- [2026-06-12] 追加: obsidian_sync.py 新規作成（DEPT-2 / GitHub出力→iCloud vault同期: pull・審議キュー自動生成・判断台帳→investment_journal記帳・成績還流。承認待ち部分一致バグを完全一致判定で修正済み）
- [2026-06-12] 追加: tests/ 新規作成（TEST-1 / conftest+7テストファイル50ケース全通過・外部API接続ゼロ。finance_logic/signal_dedup/size_decay/score_explainer/module_ledger/obsidian_sync/strategy_output_schema）

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
