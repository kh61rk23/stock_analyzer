# 保守セッションパケット（自動生成）

> 生成: 2026-07-13 08:03:54 / maintenance_packet.py（手動編集禁止）
> AIエージェントは改修着手前に本パケットを読了すること。
> 標準手順: リポジトリ直下 CLAUDE.md / 設計正本: SPEC_core.md

## ① 監査結果

### ✅ SSOT監査（config_audit）（exit 0）
    [D] 存在しない cfg 属性参照（WARNING）: 0ファイル
    ============================================================
    ✅ CRITICAL なし

### ✅ パイプライン整合（pipeline_registry_gen --audit）（exit 0）
    パイプライン監査: ステージ100 / 問題0件

### ✅ results契約（results_schema --audit）（exit 0）
    resultsスキーマ監査: 契約138キー / 実コード139キー
      ✅ 契約とコードは完全一致

### ✅ 回帰テスト（pytest tests/）（exit 0）
    ........................................................................ [ 87%]
    ..........................................                               [100%]
    330 passed in 0.49s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-07-12] 修正/追加/決定: 意思決定一括回答の反映＋Actions永続化の重大修正＋UI説明書。①mermaid構文エラー修正: UML_シーケンス図集の図3（<ticker>がHTMLタグ解釈→除去）と図6-3（日本語状態ID→as別名化）、図5のp<0.10も予防修正。②**daily_run.yml重大修正**: git永続化リストにWave R0〜R5の状態ファイル6点が欠落していた（edinet_retry_queue.json/exit_engine_baseline.json/dd_state.json/paper//cache/taxonomy//data/ml_dataset/）→追加。未修正だとML特徴量・S2口座・DD peak・hotnessベースライン・繰越キューが毎朝リセットされ蓄積が始まらなかった。requirements.txtにPyYAML明示（従来はtransformers経由の間接依存頼み）。setup_us_bq.yml/setup_subtheme_bq.ymlは変更不要と確認。③意思決定の実装: V-01=a（config BLOCK 39をローカルvaultへ是正＋iCloud側Vault2の2箇所に凍結README設置・実パス解決を検証済み）／D-02=b（S8_LLMニュースアルファ週次手動_手順書_20260712.md新規＋data/llm_news_signal.json雛形。signal_aggregator配線は4週蓄積後にD-19起票と明記）／D-03承認（判断台帳へ「S12信用見送り確定」を記録→R5-3は実装対象外）／D-08一括承認（86銘柄）／D-10=a（健太がIBKR開設→完了後R4-3着手）／D-11=a（オプションはカジノ枠内）。④説明書_リスクダッシュボードUI_20260712.md新規（S9の6パネルの見方・halt_flagトグル・トラブルシュート）。⑤MODULE_DISCOVERY_GUIDE.md更新（148→217ファイル・MAINTENANCE_GUIDEへの導線追記・現役継続=bk不要）。⑥健太がHANDOFF4本を01/bk/へ移動→残プッシュ一覧の正本はTODO_LIST §0.5へ一本化（追加プッシュ4ファイルを記載）。検証: pytest 325件全通過・config_audit 0・maintenance_packet総合判定✅。
- [2026-07-12] 設計/棚卸し: 全体棚卸し＋ドキュメント体系の整備（コード変更なし）。①新規: 02_/UML_シーケンス図集_20260712.md（正本宣言・デプロイ図・日次バッチ/B部門審議/MLメタの各シーケンス・戦略ユニット構成図・状態機械3種[dd_controller/playbook/taxonomyノード]・検証コマンドの型。Vault2 departments-Bとの物理パス一覧を明記）／02_/MAINTENANCE_GUIDE.md（「何を変えたい→どこを触る→どう検証」の逆引き10節＋禁止事項）。②更新: 02_/Overhead view_20260712.md 全面更新（100ステージ・23カテゴリ・BQ25表・R0〜R5-2Aレイヤ反映。旧20260704版は02_/bk/へ退避）／SPEC_investment_dept.md（vault二重の実態・R3-2/二層運用/exporter v2を追記）／TODO_LIST.md §0.2新設（残意思決定の正本表: V-01/D-02/D-03/D-08/D-10/D-11/D-17/D-18＋決定済み一覧＋将来の承認ゲート）。③発見（重要）: vault書き先分裂リスク=V-01。実働正本はローカル側Vault2_ClaudeCode_Localだが config BLOCK 39 既定値は2026-06-12凍結のiCloud側を指す→V-01決定までexporter/scheduler/obsidian_syncのデフォルト実行禁止。obsidian_syncはlaunchdロード済みだが両vaultに05_最新データが不存在=実効未検証。launchd invest-weeklyは未ロード（plistのみ）。④確認: D-09はNotion廃止で決定済み（config L4304-4306に記録・memory側の旧TODO「NOTION_API_KEY登録」は失効）。
- [2026-07-12] 追加/設計: Wave R4-4・R5-1・R5-2 Phase A 実装（R4-3/R5-3/R5-4は要件どおり保留）。①R4-4(S11マルチ戦略資金配分メタ): allocation_meta.py 完成（前セッション書きかけを仕上げ: build_report L215の未解決プレースホルダ{getattr}バグ修正→plan["min_months"]経由に是正）＋tests/test_allocation_meta.py 新規17件（確定式=分数ケリー0.25×リスクパリティ・カジノ枠制約充足・PF<1.0かつp>0.5淘汰・データ不足保留・レポート回帰）。config BLOCK 50は既存。main配線なし（R4-2と同じ月次スタンドアロン: python3 allocation_meta.py YYYYMM）。②R5-1(S7 MLメタモデル): ml/ 新規パッケージ=dataset_builder.py（日次で23カテゴリ+レジーム4象限+size_tier+業種を features.jsonl へ冪等追記・targetはColabで後付け）・purged_cv.py（Purged Walk-Forward CV embargo20営業日・rank IC・t統計＝純Python・Colabと共通中核）・corrector_applier.py（補正テーブル適用時にも昇格基準IC>=0.02かつt>=2.0を再検証・decile→composite±10pt上限・ML_META_ENABLED=True時のみ）・train_meta.ipynb（Colab週次LightGBM学習→昇格合格時のみJSONエクスポート）。config BLOCK 51へ ML_META_N_DECILES/CV_FOLDS/DATASET_PATH/CORRECTION_PATH/DATASET_MAX_DAYS/ENABLE_ML_META_DATASET 追記＋DAILY_MODULESに ml_meta 追加。mainに2ステージ追加（snapshot=常時軽量・corrector=フラグ時のみ）→results_schema に ml_meta_dataset/ml_meta_correction。tests/test_ml_meta.py 新規22件（受入条件=リーク日付シフトテスト・embargo有無で分割が変わる/削減幅=horizon+embargo=40日・末尾H日は目的変数なし・snapshot冪等・±10pt超テーブル適用拒否）。③R5-2 Phase A(E10 US同等化): score_calculator.py 新規（SPEC_core §8-2）＋config BLOCK 56（ENABLE_SCORE_CALCULATOR/UNIFIED_SCORE_US_TOP_N=50）＋DAILY_MODULESに score_calculator。adapterは既存スコアの写像のみ（JP=screener候補→sectors業種フォールバック・US=us_screener）で再計算なし=SSOT分裂回避。対象=保有＋US上位50。JP負域-30..0は0..23へ線形正規化。results_schema に unified_scores。tests/test_score_calculator.py 新規13件。SPEC_core §8-3にPhase A設計判断を追記。検証: pytest 325件全通過（+52）/ config_audit 0 / registry 100ステージ・監査問題0 / results_schema 完全一致(139キー) / pyflakes新規0件。未検証（人間）: 次回Actionsログ「[ml_meta_dataset]」「[score_calculator]」/ features.jsonl 蓄積確認（学習は90日以上蓄積後にColabで）/ allocation_meta はS2成績3ヶ月蓄積後に月次実行。保留: R4-3(S4オプション)=D-10 IBKR口座開設待ち・R5-3(S12信用)=D-03決議まで着手禁止・R5-4(S8)=D-02まで凍結。
- [2026-07-10] 追加/設計/整理: Wave R4-1・R4-2 実装 ＋ 既存デッドコード掃除。①R4-1(S5イベントドリブン): playbooks/（YAML外部化・4本: pead_long_v1/earnings_beat_momentum/tdnet_buyback/us8k_ceo_change_short_watch[監視のみ]）＋event_strategy_runner.py新規＋config BLOCK 45。プレイブックはstatus=paperのみ稼働・side=WATCHは注文なし。condition評価はeval不使用の構造化（field/op/value連言）で安全。event_source別extractor（pead_monitor/earnings_surprise/tdnet_monitor/us_8k_monitor）→統一候補形式。make_playbook_adapterでS2paper adapter互換（paper/strategy_adapters に get_all_adapters を追加し ENABLE_EVENT_STRATEGY時にplaybook自動登録・paper_brokerが参照）。実弾昇格M2(取引数>=30・PF>=1.3・p<0.10・健太承認)はconfig予約。②R4-2(S6コモディティローテーション): commodity_rotation.py新規＋config BLOCK 46（CMD_ETF_UNIVERSE=1326/1540/1671/1684/1687・CMD_REGIME_ALLOW=象限別許可カテゴリ）。12-1モメンタム(252-21日)×economic_cycle4象限(A:energy/grains・C:gold/broad・B/D:現金)→絶対モメンタム>0の許可資産へ等ウェイト配分（合計≤総資産10%）→注文票+レポート。発注は健太手動(D-01)。S1バックテストは backtest.cli で健太実行。③既存デッドコード掃除: position_sizer.py（未使用import json・format_position_reportの未使用ローカル変数atr/stop/risk_a/atr_p/stop_p削除）・alpha_attributer.py（プレースホルダ無しf-string修正）。※stock_analyzer_main.pyの残デッドコード(bc/tier/_ae等)は6300行巨大ファイルのデグレ回避のため据置(無害)。検証: pytest 260件全通過（+32: test_event_strategy 22・test_commodity_rotation 10）/ config_audit 0 / registry 97ステージ / results_schema 完全一致 / pyflakes新規0件。未検証（人間）: playbook実データentry/exit（次回paper稼働）/ S1でpead_long_v1・commodity 2〜3年バックテスト（backtest.cli）。**次はR4-3(S4オプション・D-10 IBKR口座前提)・R4-4(S11資金配分)。**
- [2026-07-10] 追加/設計: Wave R3-3・R3-4・R3-5 実装完了（Wave R3=審議・判断強化を完遂）。①R3-3(E9 DD制御): dd_controller.py 新規＋config BLOCK 49（D-16=c確定: DD_CAUTION=0.15/DD_DEFENSE=0.25/DD_CAUTION_EXIT=0.10/DD_DEFENSE_EXIT=0.20）＋mainに日次ステージ追加（paper_brokerの直前）→registry 97ステージ＋results_schema に dd_state 追加。equity=portfolio_holdings×当日価格の投資部分のみ（DR3-2推奨案=単一state）・peak永続化(stock_data/data/dd_state.json)・dd=1-equity/peak。next_state はヒステリシス付き状態機械（NORMAL/CAUTION/DEFENSE・未知状態はNORMALで安全再開）。売却強制なし（推奨のみ=D-01）・価格取得不能は簿価で保守評価。dd_state.json破損時はNORMAL/peak=当日equityから再開。②R3-4(E12月次戦略レビュー): monthly_strategy_review.py 新規（月初）。self_evaluator(performance_log スコア帯別的中率)/E4(signal_culling_YYYYMM.md提案件数)/判断台帳当月成績/learning_audit_log重み変更履歴/S2 paper戦略別成績 を集約→B-investment/30_レポート/strategy_review_YYYYMM.md（vault不可視時insights/）。LLM不使用テンプレ整形・欠損は「データなし」。③R3-5(S10税・NISA最適化): tax_optimizer.py 新規（四半期＋11月末）＋config BLOCK 52。①損出し候補=特定口座の含み損×当年実現益→節税額=min(Σ含み損,実現益)×20.315%を銘柄別列挙（wash sale注意文添付・NISA口座の含み損は損益通算対象外）②口座区分推奨=nisa_recommendation（NISA枠残×想定保有>1年）③旧NISA期限(2040)カウントダウン④NISA成長投資枠使用状況。税務の最終判断は健太を明記・自動発注なし(D-01)。検証: pytest 228件全通過（+34: test_dd_controller 15[6状態遷移全パス＋破損再開]・test_monthly_review 8・test_tax_optimizer 11[複数実現益×複数含み損の損益通算]）/ config_audit 0 / registry 97ステージ / results_schema 完全一致 / pyflakes新規0件。test_dd_controller はautouse fixtureで fetch_price_safe をmonkeypatch（ネットワークゼロ規約遵守・48秒→0.07秒）。意思決定DR3-2推奨案採用。未検証（人間）: DD制御の本番初回動作（次回Actionsログ「[dd_controller]」）/ 月次レビュー・税レポートの実データ生成。**Wave R3完全終了→次はR4（新戦略ペーパー検証）。**
- [2026-07-10] 追加/設計: Wave R3-1・R3-2 実装 ＋ TODO§3残タスク配線完了。①TODO§3-1(T-3消費配線): taxonomy_hotness.py に fetch_sector_node_weights を追加し、業種スコア算出を「_SECTOR_TO_TAXONOMY均等加重」から「get_company_taxonomy_scores(sector擬似ID)の company_taxonomy_map 関与度加重」に精緻化。これが空スタブ実装後も呼出元ゼロだった get_company_taxonomy_scores の唯一の日次呼出元（HANDOFF §4-8のT-3「呼出元0」を解消）。BQ空/失敗時は従来の均等加重にフォールバック。sector_scores_from_hotness を dict(加重)/list(均等)両対応に拡張＋config BLOCK55 に TAXONOMY_MARKET_DEFAULT 追加。二重計上回避のため base_agent composite には配線せず signal_aggregator カテゴリV内で完結（推奨案）。②TODO§3-3(us BQ移行): job_posting_monitor/executive_transition_tracker/facility_expansion_monitor の us.*3表INSERTはR0-2で実装済みと確認（ENABLE_US_BQ_EXPORT・insert_rows(table_ref="us.job_signal_cache"等)）→TODO消し込みのみ。③R3-1(exporter v2＋E8): agent_packet_exporter を v2化。collect_ontology_events（ontology.signal_event 直近30日をtickerで取得・時系列テーブル）＋collect_valuation（⑫E8: DCF3シナリオ=dcf_valuation_cache の min/base/max_price=弱気/基本/強気、comps相対=comps_analyzer_cache の per_rank/pbr_rank/comps_score、scenario別損益=scenario_analysis_cache の bear/bull pnl_pct。各last_updated=mtime付・欠損は「データなし」表示でパケットを壊さない）を build_packet に追加（既存best-effortパターン踏襲・後方互換）。④R3-2(E3隔週審議・軽量版=DR3-1推奨案): deliberation_scheduler.py 新規（審議対象選定=screener上位×未審議[40_判断台帳除外]・agent_packet_exporter連携・B1〜B4起動プロンプト生成[相互不可視/D-12モデル既定=ブル,ベア:Sonnet5・デビル,レポーター:Opus4.8/並列→順序制約]）＋手順書「B部門隔週自動審議_手順書_20260710.md」（スケジュール登録は健太）。検証: pytest 194件全通過（+16: test_packet_exporter_v2 6・test_deliberation_scheduler 9・test_taxonomy_wiring +1）/ config_audit 0 / registry 96ステージ / results_schema 完全一致 / pyflakes新規0件（agent_packet_exporterの既存未使用import sys も除去）。意思決定はDR3-1〜DR3-5すべて推奨案採用。未検証（人間）: exporter v2の実BQ ontology取得（要ネットワーク）/ deliberation 1回完走（Cowork）/ T-3加重のBQ実効（company_taxonomy_map投入後）。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
