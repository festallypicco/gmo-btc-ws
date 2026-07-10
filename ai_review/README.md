# AI Review Nightly Pipeline

## Overview
- Task Scheduler から呼ばれるエントリーポイントは `ai_review/run_nightly_review.ps1`。
- 現在は `build_ai_review_summary.py` -> `review_pipeline.py` の順に実行する。
- タスク登録は固定のままで運用できる。

## Manual Run
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\ai_review\run_nightly_review.ps1"
```

## Task Scheduler Registration Example
```powershell
schtasks /Create /TN "BTC_AI_Nightly_Review" /SC DAILY /ST 00:30 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\tai_m\Cursor\Projects\gmo-btc-ws\ai_review\run_nightly_review.ps1\"" /F
```

## Task Manual Trigger
```powershell
schtasks /Run /TN "BTC_AI_Nightly_Review"
```

## Optional Check / Delete
```powershell
schtasks /Query /TN "BTC_AI_Nightly_Review" /V /FO LIST
schtasks /Delete /TN "BTC_AI_Nightly_Review" /F
```

## Logs
- 実行ログ: `log/ai_review_run_YYYY-MM-DD.log`
- 集計JSON: `log/ai_review_summary_YYYY-MM-DD.json`
- AI議論結果: `log/ai_review_decision_YYYY-MM-DD.json`
- バリデーション失敗履歴: `log/ai_validation_failures.jsonl`

## Guardrails (2段ガード)
- ① **プロファイル名セット一致チェック**:
  - `review_pipeline.py` は、現行 `config/config.json` のプロファイル名集合と、AI提案のプロファイル名集合が完全一致しない場合、更新を適用しない。
  - 目的は、**構成変更（追加/削除/改名）をAIだけで自動反映せず、人間確認を必須にする**こと。
  - このとき `decision_doc.status` は `"held_profile_name_mismatch"` になる。
- ② **絶対範囲チェック（PARAMETER_ABSOLUTE_BOUNDS）**:
  - 既存の相対±15%チェックの後段で、各パラメータを絶対範囲に機械的にクランプする。
  - 目的は、**明らかに不適切な値を弾く最終防波堤**として機能させること。
  - 補正された項目は `clamped_to_bounds_fields` に記録される。

### PARAMETER_ABSOLUTE_BOUNDS の位置づけ
- `PARAMETER_ABSOLUTE_BOUNDS` の値は**初期案**。
- 実運用での実績（取引品質、過補正の有無、市場環境の変化）を見ながら、定期的に見直す前提。

## 運用対応: held_profile_name_mismatch
- `log/ai_review_decision_YYYY-MM-DD.json` で `status: "held_profile_name_mismatch"` を検知したら、まず `added_names` / `removed_names` を確認する。
- 次に、Proposer/Skeptic/Moderator の出力内容を読み、**プロファイル名変更の意図が妥当か**（構成見直しの必要性が本当にあるか）を人間が判断する。
- 妥当と判断した場合のみ、`config/config.json` を手動編集してプロファイル名変更を反映し、必要に応じて関連パラメータを調整する。
- 妥当でない場合はそのまま見送り、次回夜間実行に委ねる。

## 定期見直しチェックリスト（目安: 月1回）

夜間パイプラインとガードレールが意図どおり機能しているか、設定やインフラ面も含めて確認する。

### 1. 絶対範囲（PARAMETER_ABSOLUTE_BOUNDS）の妥当性

- 直近1ヶ月分の `log/ai_review_decision_YYYY-MM-DD.json` を横断し、`clamped_to_bounds_fields` の出現頻度を確認する。
- 特定パラメータ（例: `daytime.take_profit_pct`）が**頻繁に**補正されている場合:
  - 範囲が**狭すぎて**正当な提案まで切り詰めている可能性 → 上限/下限の緩和を検討
  - 逆に、補正がほぼ出ないのに市場環境が大きく変わっている場合 → 範囲が**広すぎて**防波堤になっていない可能性 → 見直しを検討
- 変更する場合は `btc_trading_tool/config_manager.py` の `PARAMETER_ABSOLUTE_BOUNDS` を更新し、変更理由をメモしておく。

### 2. held_profile_name_mismatch の発生確認

- 直近1ヶ月で `status: "held_profile_name_mismatch"` が何回出たか数える。
- **繰り返し**同種の改名・追加・削除提案（例: `night` → `night_v2`）が出ていないか確認する。
- 判断の目安:
  - **ノイズ**: 根拠が弱く、1回きりまたは理由が毎回バラバラ → 現行プロファイル構成を維持
  - **本当に見直すべきタイミング**: 成績差・レジーム変化・時間帯の定義が実態とずれている、など一貫した根拠がある → 人間が `config/config.json` を手動で再構成

### 3. ai_validation_failures.jsonl の傾向確認

- `log/ai_validation_failures.jsonl` の直近エントリを確認する。
- Moderator の3回リトライや `past_validation_failures` の記憶があっても**同じエラーパターンが繰り返し**出ていないか見る（JSONパース失敗、プロファイル連続性エラー、必須フィールド欠落など）。
- 繰り返し系がある場合:
  - プロンプト（`prompts.py`）の指示不足
  - サマリーJSONの形式・不足データ
  - モデル側の出力形式の癖  
  など、原因を切り分けて修正を検討する。

### 4. 時間帯プロファイルそのものの妥当性

- 現行の `early_morning` / `daytime` / `night` という区切りが、**実際の成績差**を反映しているか再検証する。
- 参照データ:
  - `log/ai_review_summary_*.json` の `windows.*.per_profile`（win_rate, profit_factor, exit_count など）
  - `log/realtime_trading_log_*.csv` をプロファイル別・時間帯別に集計
- 区切りを変える前に、「時間帯別の成績差をログで検証してから決める」方針に従い、データに基づいて境界時刻（08:00 / 16:00 等）やプロファイル数を見直す。
- 構成変更は AI 自動適用ではなく、人間が `config/config.json` を手動編集する（名前不一致ガードと同じ運用）。

### 5. 無料枠の使用量確認（Groq / Gemini）

- Groq（Proposer）・Gemini（Skeptic / Moderator）の各ダッシュボードで、直近1ヶ月の**呼び出し回数・トークン量**を確認する。
- 1晩あたりの目安: Proposer 1回 + Skeptic 1回 + Moderator 最大3回（リトライ含む）。
- 無料枠に対する余裕が少なくなっている場合:
  - リトライ頻度の増加（validation 失敗が多い）が原因か確認
  - プロンプト長の削減（Proposer 用 summary の軽量化は既に `regime_reference.blocks` で実施済み）
  - 必要に応じて実行頻度・モデル選定の見直し

### 6. ログ肥大化・ディスク容量

- `log/` 配下の容量とファイル数を確認する。特に増えやすいもの:
  - `market_snapshot_YYYY-MM-DD.csv`（60秒間隔・日次）
  - `realtime_trading_log_YYYY-MM-DD.csv`
  - `ai_review_run_YYYY-MM-DD.log` / `engine_YYYY-MM-DD.log` / `restart_YYYY-MM-DD.log`
  - `ai_review_summary_*.json` / `ai_review_decision_*.json`
- 方針の例:
  - **保持期間**: 例）生ログ90日、集計JSONは1年、decision は永久または1年
  - **アーカイブ**: 古い CSV/ログを zip にまとめて別ドライブやクラウドへ退避
  - **削除**: アーカイブ済みの原件を削除
- ディスク逼迫の前に、月次チェックで傾向（1日あたりの増分 MB）を把握しておく。
