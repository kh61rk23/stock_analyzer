# stock_analyzer — AIエージェント保守手順書（AGENT-1）

このリポジトリで Claude Code 等のAIエージェントが作業する際の標準手順。
設計の正本は `../統合ファイルなど/01_最新版ロードマップ・必要書類/`
（SPEC_core.md / SPEC_db_schema.md / SPEC_investment_dept.md / MODULE_INDEX.md）。

## 絶対禁止

- **`../stock_analyzer-main_bk` を変更しない**（バックアップ。読むのは可）
- APIキー・トークンのハードコード（`os.getenv()` + GitHub Secrets 経由のみ）
- config.py のモジュールレベル重複定義（Pythonは後勝ち上書きで黙って壊れる。
  2026-06 に LEADING_INDICATOR_RULES 三重定義で先行指標7本が無効化されていた実害事例）

## セッション開始時（改修着手前に必ず）

```bash
python3 maintenance_packet.py   # 全監査+台帳+直近変更を MAINTENANCE_PACKET.md に集約
```
総合判定が ❌ なら、依頼内容より先に監査NGの修復を提案すること。

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
2. `../統合ファイルなど/01_最新版ロードマップ・必要書類/CHANGELOG.md` に
   `[YYYY-MM-DD] 種別: 内容（ファイル名必須）` 形式で追記
3. MODULE_INDEX.md / SPEC_core.md に構造変更を反映

## このシステムの構造（30秒）

- エントリ: GitHub Actions（毎朝JST6:00 / 月曜のみ自動でweeklyモード）→ run_headless.py → stock_analyzer_main.run_all_tasks()
- 設定SSOT: config.py（BLOCK 0〜39・他moduleをimportしない）
- パイプライン宣言: pipeline_registry.json（89ステージ・AST自動生成・手動編集禁止）
- データ契約: results_schema.py（127キー）
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
