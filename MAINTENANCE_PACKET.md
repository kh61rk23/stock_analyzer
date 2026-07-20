# 保守セッションパケット（自動生成）

> 生成: 2026-07-20 21:16:02 / maintenance_packet.py（手動編集禁止）
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
    ........................................................................ [ 90%]
    ....................................                                     [100%]
    396 passed in 1.46s

**総合判定: ✅ 全監査グリーン — 改修着手可**

## ② 実行台帳（直近の本番実行状態）

- 台帳なし（本番実行ログが未取得。obsidian_sync pull で取得可）

## ③ 最近の変更（CHANGELOG 直近10行）

- [2026-04-16] 追加: edinet_bq_client.py に get_company_taxonomy_scores() を追加
- [2026-04-16] 修正: base_agent.py L245付近 calc_composite_score() にtry/except追加
- [2026-04-15] バグ: alert_notifier.py Gmail送信タイムアウト → SMTP_TIMEOUT 30→60秒
- [2026-04-14] 設計: SPEC_db_schema.md 新規作成（シャルロット議論のDB設計を統合）
- [2026-07-20] 修正/検証: weekly 13FとTODO人間側CLIを一括是正。①daily_run.ymlの13F専用stepだけSTOCK_ANALYZER_BASEが欠落しconfigがColab既定 `/content/drive/...` を採用→runnerでPermissionErrorだったためenv追加。config.pyも「env→実在Colab Drive→repo/stock_data」へ三環境対応。②backtestはauto_adjust価格へ配当を二重加算し、CLIも初日終値と翌日約定を比較して誤差5.9%を誤検出→配当二重計上停止・同一約定条件比較へ修正し誤差0.0841%で受入✅。③EDINETBQClientはローカル認証JSONのproject_idを補完せず空project、taxonomy loader/mapperも空cfg値を優先→認証projectへ統一。taxonomy 293行MERGE成功。④requirementsにdb-dtypes追加。⑤allocation_metaは`--help`を月名として空提案書を作るためargparse/年月検証・S2履歴なしskipを追加。⑥dashboardはPATH非依存の`python3 -m streamlit`でHTTP 200確認。launchd登録確認。TODO_LIST同期・回帰テスト追加。
- [2026-07-20] 設計: DB統合改修（健太指示①②③）の要件定義完了（Fable/Opus設計・以後Sonnet 5で実装可）。①EDINET→EdinetDB一本化②不動産DB(fudosandb.jp)利用③米国株BQ格納。**外部API事実をキー付きREST疎通で実測確認**: EdinetDB=edinetdb.jp/v1・X-API-Key・free plan 100/日1500/月・OpenAPI公開・/companies(名寄せ)・/financials(約90項目)・/segments・/text-blocks完備／不動産DB=fudosandb.jp/v1・X-API-Key・7/30まで1000/日→8/1以降100/日・holdings検索がedinet_codeで企業結合可／米国株=既存us_fundamental_rotator(SEC EDGAR)はローカルJSONのみでBQ未格納。成果物=統合ファイルなど/05_提案_2026-07-20/(要件定義書_DB統合改修/意思決定事項D-①-1〜D-⑧/成果物・ファイル一覧/俯瞰図)。W1 db_rest_client→W2 米国株BQ(最短)→W3 EdinetDB ingestor→W4 政府EDINET撤去→W5 不動産ingestor→W6 配線の6Wave。config BLOCK 58/59/60予約。TODO_LIST §0.4新設。EdinetDB月次1500が真の律速→優先度付きローテ(保有→スクリーナー上位→セクター代表→残り)を必須設計。意思決定はW4/W6のみ承認待ち・他は推奨案で凍結済み。
- [2026-07-19] 修正/追加: 健太報告の実証①〜⑧を一括対応（Fable・BQ/GitHub両コネクタの実データで裏取り）。①api_key_health.py _probe_bq: EDINETBQClientはquery()を持たないラッパーのためAttributeError→「SELECT 1失敗（認証・権限エラーの疑い）」と誤報告していた（同一実行内でBQ MERGE 59件成功＝**IAMロールは正常・GCP追加作業不要**）。生クライアント(.client)でSELECT 1する方式へ是正＋例外型名をログ出力。②country_risk_updater.py: travel.state.gov 403（Actions IP恒常ブロック）を非リトライ化し、RSS取得失敗時は合成スコアを残ソースで再正規化（mofa_available/state_available新設。「取得失敗」を「レベル0=安全」と誤解釈しない）。③同: GDELT Doc API恒常429の代替として**BigQuery公開データセット経路をprimary実装**（gdelt-bq.gdeltv2.events_partitioned・紛争系EventRootCode 14/15/17/18/19/20の平均トーン・_ISO3_TO_FIPS 59国マップ・1クエリ数MBスキャン）。59/59国の実データ取得をMCP直queryで検証済み。失敗時のみDoc API→減衰キャッシュへフォールバック。④segment_signal.py+edinet_bq_client.py: 幻のAPI（bq.get_segment_data/bq.query）と実在しないテーブル（taxonomy.segments_master/edinet.segment_financials）参照で常に0件だった読取経路を、get_segment_finance実装（edinet.segments×segment_finance実結合）＋build_segment_view（最新期抽出・growth_rate算出の純関数）で是正。⑤edinet_direct_fetcher.py: (a)EDINET API v2のSubscription-Keyヘッダ欠落（キーがあっても401になる構造）(b)financials/segments書込行が実スキーマと全列不一致（fin_id等REQUIRED欠落）を是正し segments+segment_finance の2表書込へ（build_financials_row/build_segment_rows純関数化）→**EDINET_API_KEY再発行(⑧)後に蓄積が実際に始まる前提条件を整備**。⑥text_extractor.py: EDINETDB_API_KEYで誤ってAPIルートを起動し毎銘柄2回無駄打ちしていたゲートをEDINET_API_KEY専用へ＋キー未設定時はバッチ冒頭1行明示。⑦GitHub SHA照合: 07-19朝の健太アップロードで要プッシュ分はほぼ同期済みと確定（残=backtest/一式のみ・paper/paper/重複ネスト残骸。今朝の実行のみ旧yml定義で発火しfatalノイズが出た＝次回から解消）。tests/test_country_risk_bq_route.py＋tests/test_segment_pipeline.py 25件新規。検証: pytest 384件全通過／pyflakes新規0／TODO_LISTへ人間側コマンドのフルパス化と07-19セクション追加。
- [2026-07-16] 修正: GITHUB_SECRETS_LEDGER.md のAPIキー平文5件を健太指示で復旧（前エントリでAIが削除したもの。台帳に値も記録する運用を健太が選択・記録節に留意1行残置）。TODO_LISTのセキュリティ項目をクローズ。
- [2026-07-16] 修正/追加/セキュリティ: daily照合#3＋BQ実データ検証＋APIキー健全性チェック実装（Fable設計・Opus/Sonnet実装・Fableレビュー）。①**セキュリティ**: GITHUB_SECRETS_LEDGER.md にAPIキー平文5件が記入されていたため即削除（露出扱いの判断は健太へ・TODO_LIST起票）。②**BQ実データ検証（MCP直query）**: financials/segments/text_analysis/taxonomy_node/company_taxonomy_map=全て0行と確定→segment_signal全業種0pt・text_extractor全滅は**コードバグでなく上流データ空**（EDINET蓄積ゼロ）が原因。doc_index=193行だが全行fetched=true・disclosure_date NULLの旧データで実質未更新。country_risk_daily=1062行は正常稼働。③**「状態ファイルがGitHubに生成されない」の根本原因特定**: commitステップで「64 files changed」成功後に pull --rebase が「You have unstaged changes」で失敗→リモート先行日はpushが||trueで黙殺され**全状態ファイルがロスト**していた（実行ログで実証）。daily_run.ymlを pull --rebase --autostash 化＋push成否を明示ログ化＋git addのfatalノイズ64行を2>/dev/null抑制。④J-1[Sonnet] country_risk_updater.py GDELT v2: 最終取得値の永続キャッシュ（stock_data/cache/gdelt_instability_cache.json・yml永続化追加）→取得失敗時は日次5%減衰で代用（♻️ログ）・回路閾値5→3・JSONパース失敗も失敗カウント。tests/test_gdelt_cache.py 11件。Fable検証=減衰42.0/6日・下限0。⑤J-2[Opus] **api_key_health.py新規（506行）**: 健太要望「キー生死をログで判定」。EDINET/EdinetDB/e-Stat/FRED/DeepL/CSE(既定OFF)/Gmail SMTP/BQ を軽量1コールで✅❌⚠️⏭️判定し実行ログ冒頭に「🔑 [key_health]」サマリ＋詳細行を出力。probe部と判定部を分離した純関数設計・キー値のマスキング検査つき・config BLOCK 57・main序盤ステージ・registry 101ステージ化。tests 13件。Fable裁定3件を反映=(a)EdinetDBは認証不要searchだと偽✅→認証必須analysisへ変更（予算1コール許容） (b)EDINETキーはURLパラメータでなくヘッダOcp-Apim-Subscription-Key（既存流儀）に統一 (c)BQはEDINETBQClientクラスで固定。⑥dead_ticker_pruner v1.3: 第1パスをyf.downloadバッチ化（100銘柄/リクエスト・リクエスト数1/100）＝v1.2実行で642件が偽デッドのままガード発動した「レート制限の自己誘発」への根本対策。applyは翌日以降に健太実行。⑦GITHUB_SECRETS_LEDGER.md: 平文削除＋是正記録＋EDINETDB再発行✅/EDINET失効疑いを記録。検証: pytest 359件全通過（+24）/ config_audit 0 / registry 101・問題0 / results_schema 完全一致 / maintenance_packet✅。
- [2026-07-16] 修正: BQ 403（edinet_rotation 3日連続失敗）の根本原因是正＋pruner v1.2。①edinet_bq_client.py query_to_df: **2026-07-12に導入したgoogle-cloud-bigquery-storageが原因**。同パッケージ導入でto_dataframe()がStorage Read API経路へ自動切替され bigquery.readsessions.create 権限（roles/bigquery.readSessionUser）を新規要求→SAは書込/ジョブ権限のみで403（メール通知3日連続と導入日が一致・エラーメッセージの権限名も一致）。create_bqstorage_client=False でREST経路に固定し権限付与に依存しない構成へ（読取量的にREST十分・IAM付与後にTrueへ戻す選択肢はTODOに記載）。doc_index未更新（テーブル実在は健太がGCPで確認）も同403の巻き添えの可能性が高く、次回ログ「N件挿入」で最終確認。to_dataframeはリポジトリ全域でこの1箇所のみと確認。②dead_ticker_pruner.py v1.2: v1.1でも防げない失敗モードを実測で発見=yfinanceは失敗時に例外でなく空DataFrameを返すためunknownに落ちずdead誤分類（実害: 実行中盤のDNS断でsp500のM以降215銘柄が偽デッド・計369件）。デッド候補を60秒後に再検証する2パス方式＋再検証後もデッド率>50%ならapply拒否を標準化。v1.2で--apply再実行（結果は完了後に本CHANGELOGへ追記）。検証: pytest 335件全通過 / pyflakes新規0 / py_compile OK。

## ④ 改修時の鉄則（要約 — 詳細は CLAUDE.md）

1. `stock_analyzer-main_bk` は絶対に触らない
2. config.py 変更後は `python3 config_audit.py`（重複定義は後勝ち上書きで黙って壊れる）
3. main 変更後は `python3 pipeline_registry_gen.py` で registry 再生成
4. results キー追加時は results_schema.py の SCHEMA に追記
5. 改修完了時は pytest 全通過を確認し、CHANGELOG に1行追記
