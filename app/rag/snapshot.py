"""Compact, text holdings snapshot for the RAG prompt (spec §6).

Reuses build_dashboard so the snapshot always matches what the user sees.
Kept short (class-level targets/current/deviation + top securities) to bound
prompt tokens. Amounts are included; this never runs in shared read-only mode
(the /api/rag/* endpoints 403 there).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..services import build_dashboard


def holdings_snapshot(db: Session) -> str:
    payload = build_dashboard(db, readonly=False, hide_amounts=False)
    if not payload:
        return "（暂无持仓数据）"
    lines = []
    total = payload.get("total_assets")
    if total is not None:
        lines.append(f"总资产：约 {total:,.0f} 元")
    for ac in payload.get("asset_classes", []):
        cur = ac.get("current_weight")
        tgt = ac.get("target_weight")
        dev = ac.get("deviation")
        cur_s = f"{cur:.1f}%" if cur is not None else "—"
        dev_s = f"{dev:+.1f}%" if dev is not None else "—"
        names = "、".join(
            s["name"] for s in ac.get("securities", [])[:4] if s.get("shares")
        )
        line = f"- {ac['name']}：目标 {tgt:.0f}% / 当前 {cur_s}（偏离 {dev_s}）"
        if names:
            line += f"；持有：{names}"
        lines.append(line)
    return "\n".join(lines)
