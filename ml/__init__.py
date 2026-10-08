# ==============================================================
# ml/ — MLメタモデル（R5-1 / S7）
# ==============================================================
# 設計正本: 要件定義書_統合改修_20260705.md §R5-1。
#   - dataset_builder : 日次に特徴量スナップショットを features.jsonl へ追記
#   - purged_cv       : Purged Walk-Forward CV / rank IC（純粋ロジック・pytest対象）
#   - corrector_applier: 補正テーブル（十分位→composite加減点）の日次適用
#   - train_meta.ipynb: Colab週次学習（LightGBM）。補正テーブルをJSONエクスポート
# 原則: composite への補正は ML_META_ENABLED=True かつユーザー承認後のみ。
# ==============================================================
