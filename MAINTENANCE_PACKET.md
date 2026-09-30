# 保守セッションパケット（自動生成）

> 生成: 2026-09-30 15:29:27 / maintenance_packet.py（手動編集禁止）
> AIエージェントは改修着手前に本パケットを読了すること。
> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md

## ① 監査結果

### ✅ SSOT監査（config_audit）（exit 0）
          L4689: TAXONOMY_MARKET_DEFAULT
    ============================================================
    ✅ CRITICAL なし

### ✅ パイプライン整合（pipeline_registry_gen --audit）（exit 0）
    パイプライン監査: ステージ108 / 問題0件

### ✅ results契約（results_schema --audit）（exit 0）
    resultsスキーマ監査: 契約147キー / 実コード148キー
      ✅ 契約とコードは完全一致

### ✅ 回帰テスト（pytest tests/）（exit 0）
    ........................................................................ [ 98%]
    .................................                                        [100%]
    2192 passed, 1 skipped in 8.31s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 2026-09-29: 成功69 / 失敗0 / スキップ3

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-09-21] 修正: **オプション戦略の提案が毎日0件**（options_strategy_advisor.py / config.py・健太の判断）。実行時刻は常に米国市場の時間外で、yfinance の ATM impliedVolatility が 0.0001 等で返るため「IV欠損」で全件落ちていた（9/18 実測 IV欠損5件）。実行時刻は変えず、HV（ヒストリカル・ボラティリティ）の過去1年分布での分位で環境を判定する方式に変更（`OPTIONS_HV_RICH_PCTL`=70 / `OPTIONS_HV_CHEAP_PCTL`=30、旧 `OPTIONS_IV_*_RATIO` は削除）。IV は参考表示のみ。実測: SPY は HV 9.2%・10分位＝低ボラ → コール買いの提案が出た。
- [2026-09-21] 修正: **JPX信用残高が毎週 yfinance の代理値に落ちていた**（credit_data.py / config.py）。従来の CSV（margin/tvdivq0000001mf0-att/data_j.csv）は配信終了で404。現行は「銘柄別信用取引週末残高」がPDF（syumatsuYYYYMMDD00.pdf・毎週火曜16:30目安）で掲載されるため、一覧ページ（margin/05.html）から最新URLを見つけて PyMuPDF で解析する方式に変更（実データ 4,194銘柄・トヨタ6.57倍/MUFG21.1倍を確認）。信用倍率は市場全体では中央値56.7倍で、絶対閾値（>10倍=過熱）だと2,905銘柄が減点対象になるため、健太の判断でその週の分位判定（下位10%=踏み上げ・上位10%=過熱・上位25%=注意）に変更し、売残ゼロの銘柄は判定対象外とした（踏み上げ225 / 過熱224 / 注意334 / 通常3,411）。回帰テスト tests/test_fixes_0921.py（7件）、全体 2148 passed。コミット c4d716a。
- [2026-09-21] バグ: **週次モジュール4件が読み先・実行順のずれで毎回空振り**（score_history_io.py / portfolio_optimizer.py / backtester.py / performance_tracker.py / stock_analyzer_main.py）。①score_history_io は insights/score_history.json を探すが実際の保存先は DATA_DIR/score_history.json（実ファイルは89日分）。jp_us_sector_bridge・self_evaluator・screener_sector_heatmap が毎週「score_history.json なし」でスキップしていた。②portfolio_optimizer の保有CSV候補に cfg.PORTFOLIO_HOLDINGS_PATH が無く、18銘柄のファイルがあるのに毎週スキップ（exit_rule_engine / scenario_analysis は読めていた）。③screener_history にエントリー価格も pass_filters も保存しておらず、バックテストと自己評価が常に0件 → 保存項目に追加し、過去分（フラグ欄なし）は通過扱いで読む。④portfolio_diversity_filter が screener の前に置かれ results["screener"] が常に空だったため、スクリーニング直後へ移動。
- [2026-09-21] 修正: **taxonomy の業種スコアが毎日フォールバックだった**（taxonomy_hotness.py / edinet_bq_client.py / config.py）。company_taxonomy_map のキーは4桁の企業コードなのに業種コード（および "sector:JP:xxxx" の擬似ID）で引いており該当行が無く、_SECTOR_TO_TAXONOMY の均等加重に落ちていた（9/21 は業種コードが偶然1社のコードと一致して「1業種」）。さらに業種ごとに1本ずつ引く N+1（9本/日）。業種→銘柄（1本）＋銘柄の関与度（1本・get_taxonomy_scores_bulk を追加）の計2本に集約。実データで1業種あたり4〜11ノード。
- [2026-09-21] 修正: **スクリーニング候補ビルダーの動的追加が毎日0社**（screener_candidate_builder.py・健太の判断）。読んでいたキー（high_priority / alerts / disclosures）が tdnet_monitor の返り値（events / all_events）と違い、TDnet モニターはビルダーより後に動くため当日の results も空だった。前回実行の通知済みキャッシュから直近3日の自己株買い・増配・MBO・大量保有を拾う（実データで14社）。
- [2026-09-21] 検証: **9/19 の修正が週次実行で効いたことを確認**（Actions 35562458084 / メール 9/21）。モメンタム補正は ±10点25業種 → ±5点13業種のみ、NEDO採択スクレイパーが 採択23件・名寄せ5件・BQ格納✅（従来0件）、TDnet は総検知18件・新規0件（重複判定が機能）、EDINET品質付与 30/30銘柄、sector_tier TOP11/MID11/BOT11、注目ニュース5件はすべて市場・企業ニュース、異常検知の「スコア急変」HIGH は消滅。ニュースは週末のため公式系（SEC/Fed/ECB/日銀/財務省）が0件（平日の単体確認では SEC 30件・財務省14件・Arabian 30件・Globes 3件）。回帰テスト tests/test_fixes_0919.py（21件）、全体 2141 passed。コミット 3d31beb / 20d326e / a70e82a ほか。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
