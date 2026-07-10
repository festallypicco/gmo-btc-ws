from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _pct_text(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.1f}%"


def _format_recent_change_outcomes(outcomes: Any, limit: int = 5) -> str:
    if not isinstance(outcomes, list):
        return "- none"
    lines = []
    for row in outcomes[-limit:]:
        if not isinstance(row, dict):
            continue
        final_eval = row.get("final_evaluation") if isinstance(row.get("final_evaluation"), dict) else None
        provisional_eval = (
            row.get("provisional_evaluation")
            if isinstance(row.get("provisional_evaluation"), dict)
            else None
        )
        selected = final_eval or provisional_eval
        if selected is None:
            continue
        comparison = selected.get("comparison", {}) if isinstance(selected.get("comparison"), dict) else {}
        confidence = selected.get("confidence", "insufficient")
        label = "最終" if final_eval else "暫定"
        change_ts = str(row.get("timestamp", ""))[:10]
        profile = str(row.get("profile_name", "unknown"))
        changed_fields = row.get("changed_fields", {})
        if isinstance(changed_fields, dict) and changed_fields:
            field_name = sorted(changed_fields.keys())[0]
            field_delta = changed_fields.get(field_name, {})
            if isinstance(field_delta, dict):
                old = field_delta.get("old")
                new = field_delta.get("new")
                change_desc = f"{field_name} {old}->{new}"
            else:
                change_desc = field_name
        else:
            change_desc = row.get("change_type", "change")
        win_rate_diff = _pct_text(comparison.get("win_rate_diff"))
        trade_diff = _pct_text(comparison.get("trade_count_diff_pct"))
        pnl_diff = comparison.get("total_pnl_diff")
        pnl_text = "n/a" if pnl_diff is None else f"{pnl_diff:+,.0f}円"
        lines.append(
            f"- {change_ts} {profile}: {change_desc} -> 勝率{win_rate_diff}、"
            f"取引頻度{trade_diff}、PnL差{pnl_text}（confidence: {confidence}、{label}）"
        )
    if not lines:
        return "- none"
    return "\n".join(lines)


def _truncate_last_output_for_proposer(value: Any, max_chars: int = 300) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "...(truncated)"


def _past_validation_failures_for_proposer(failures: Any, limit: int) -> Any:
    if not isinstance(failures, list):
        return failures
    selected = failures[-limit:] if limit > 0 else []
    reduced: list[Any] = []
    for row in selected:
        if not isinstance(row, dict):
            reduced.append(row)
            continue
        item = dict(row)
        if "last_output" in item:
            item["last_output"] = _truncate_last_output_for_proposer(item.get("last_output"))
        reduced.append(item)
    return reduced


def _build_proposer_reduced_summary(summary: Dict[str, Any], failures_limit: int) -> Dict[str, Any]:
    return {
        "target_date": summary.get("target_date"),
        "current_config": summary.get("current_config"),
        "recent_config_changes": summary.get("recent_config_changes"),
        "past_validation_failures": _past_validation_failures_for_proposer(
            summary.get("past_validation_failures"),
            failures_limit,
        ),
        "windows": {
            "anomaly_check": summary.get("windows", {}).get("anomaly_check"),
            "rule_review": summary.get("windows", {}).get("rule_review"),
            "stability_check": summary.get("windows", {}).get("stability_check"),
            "regime_reference": {
                "requested_days": summary.get("windows", {}).get("regime_reference", {}).get("requested_days"),
                "actual_days": summary.get("windows", {}).get("regime_reference", {}).get("actual_days"),
                "blocks": summary.get("windows", {}).get("regime_reference", {}).get("blocks", []),
                "summary": summary.get("windows", {}).get("regime_reference", {}).get("summary"),
            },
        },
    }


def _assemble_proposer_prompt(summary: Dict[str, Any], failures_limit: int) -> str:
    reduced_summary = _build_proposer_reduced_summary(summary, failures_limit)
    recent_change_outcomes_text = _format_recent_change_outcomes(
        summary.get("recent_change_outcomes", []),
        limit=5,
    )
    return (
        "以下の集計サマリーを元に、設定変更案を提案してください。\n\n"
        "[Recent Change Outcomes]\n"
        f"{recent_change_outcomes_text}\n\n"
        f"{_json(reduced_summary)}\n"
    )


def build_proposer_prompt(summary: Dict[str, Any]) -> Tuple[str, str]:
    system = (
        "あなたは提案役（Proposer）です。"
        "直近14日の rule_review を主判断材料にし、anomaly_check（前日1日）は単体で"
        "ルール変更判断に使わないでください。"
        "提案ごとに対象プロファイル名・パラメータ名・変更前後の値・根拠となる具体的数値を"
        "必ず明記してください。"
        "出力は簡潔にまとめ、各提案は1〜2行で記述してください。"
        "期待効果・実装上の注意点・モニタリング方針などの長文の作文は不要です。"
        "全体で概ね1500トークン以内に収めてください。"
        "絵文字や装飾的な記号（丸囲み数字など）は使わないでください。"
        "past_validation_failures にある失敗パターンを繰り返さないでください。"
        "max_order_size_btc は板の厚み(best_bid_size/best_ask_size)と約定しやすさに直結するため、"
        "引き上げ提案は流動性リスクを明示して慎重に行ってください。"
    )

    prompt = _assemble_proposer_prompt(summary, failures_limit=5)
    if len(prompt) > 12000:
        prompt = _assemble_proposer_prompt(summary, failures_limit=2)
    return system, prompt


def build_skeptic_prompt(summary: Dict[str, Any], proposer_output: str) -> Tuple[str, str]:
    system = (
        "あなたは懐疑役（Skeptic）です。提案をそのまま通さず、弱点を検証してください。"
        "以下を必ず確認してください: "
        "1) confidence が insufficient/low のデータに依存しすぎていないか。"
        "2) 14日傾向が 30日 weekly_breakdown で再現しているか。"
        "3) 90日のレジーム変化（dailyベース）に対して過学習していないか。"
        "4) max_order_size_btc の引き上げ提案は、板の厚みと流動性リスクを十分に説明しているか。"
    )
    prompt = (
        "集計サマリー（フル）と Proposer 出力を評価し、"
        "具体的な反証・注意点・条件付き承認案を提示してください。\n\n"
        f"[Summary]\n{_json(summary)}\n\n"
        f"[Proposer]\n{proposer_output}\n"
    )
    return system, prompt


def build_moderator_prompt(
    summary: Dict[str, Any],
    proposer_output: str,
    skeptic_output: str,
    retry_context: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    system = (
        "あなたは最終決定役（Moderator）です。"
        "必ず JSON のみを出力してください。説明文、前置き、Markdownコードブロックは禁止です。"
        "出力スキーマは次の通りです: "
        '{"updated_reason":"...", "profiles":[{"name":"...","start_time":"...","end_time":"...",'
        '"imbalance_entry_threshold":0.0,"min_entry_wall_btc":0.0,"min_valid_wall_btc":0.0,'
        '"max_spread_pct":0.0,"max_allowed_spread":0.0,"imbalance_cancel_threshold":0.0,'
        '"take_profit_pct":0.0,"stop_loss_pct":0.0,"maker_price_offset_jpy":0.0,'
        '"max_order_size_btc":0.0,"daily_target_order_size_btc":0.0,'
        '"daily_target_order_size_reasoning":"..."}]}.'
    )
    retry_header = ""
    if retry_context:
        retry_header = (
            "前回出力はバリデーションに失敗しました。"
            "以下のエラーを修正して、再度 JSON のみを出力してください。\n"
            f"前回の出力: {retry_context.get('last_output', '')}\n"
            f"バリデーションエラー: {retry_context.get('error', '')}\n\n"
        )

    prompt = (
        f"{retry_header}"
        "Summary / Proposer / Skeptic を統合し、最終設定JSONを返してください。\n\n"
        f"[Summary]\n{_json(summary)}\n\n"
        f"[Proposer]\n{proposer_output}\n\n"
        f"[Skeptic]\n{skeptic_output}\n"
    )
    return system, prompt
