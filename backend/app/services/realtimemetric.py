"""实时指标业务规则：状态流转、字段校验与指标窗口聚合记录写入都收在这里。

修复要点：
- 登记时全字段落库，聚合函数、数据源流等不再丢失（与大盘配置丢失字段同属一个根因）；
- 状态只能沿 草稿 → 定义评审中 → 已上线 → 已下线 逐段推进，不能跳段、不能倒回；
- 指标下线会同步清理所有大盘的指标组引用，并留下可解释的异常卡片；
- 别的页面产生的实时指标结果同步写入指标窗口聚合记录集，补上调用链上的数据缺口。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "realtimemetric"
WINDOW_MODULE = "metricwindow"  # 指标窗口聚合记录集
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算窗口秒"]
ALL_FIELDS = ["指标编号", "指标名称", "计算窗口秒", "聚合函数", "数据源流", "维度字段组", "延迟容忍毫秒", "精度位数"]
STATUS_ORDER = ["草稿", "定义评审中", "已上线", "已下线"]
# 状态机：动作 -> (允许的前置状态, 目标状态)。生命周期只能逐段前进。
TRANSITIONS = {
    "提交定义评审": ("草稿", "定义评审中"),
    "上线指标": ("定义评审中", "已上线"),
    "下线指标": ("已上线", "已下线"),
}
ACTION_RULES = {action: target for action, (_, target) in TRANSITIONS.items()}
NEGATIVE_ACTIONS = ["下线指标"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 窗口记录取号锁：并发上报时记录编号唯一。
_window_lock = threading.Lock()


class RealtimemetricService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            row.setdefault("关联大盘", [])
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            entry.setdefault("关联大盘", [])
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 全字段落库：聚合函数、数据源流等不再在登记后丢失。
        entry.update({field: values.get(field) for field in ALL_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["关联大盘"] = []
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于实时指标可执行范围"
        source, target = TRANSITIONS[action]
        current = str(entry.get("status") or "")
        if current == target:
            return None, f"指标定义已处于「{target}」，请勿重复{action}"
        if current != source:
            return None, f"指标定义当前状态为「{current}」，须处于「{source}」才能{action}；生命周期不能跳段或倒回"
        entry["status"] = target
        entry["pending"] = target not in ("已上线", STATUS_ORDER[-1])
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        message = f"指标定义已{action}"
        if action == "下线指标":
            # 指标组引用同步：已下线指标不能残留在任何大盘的指标组里。
            from app.services.dashboard import DashboardService

            affected = DashboardService().sync_metric_offline(str(entry.get("指标编号") or ""))
            if affected:
                message += f"，已同步清理 {len(affected)} 个大盘的指标组引用：{'、'.join(affected)}"
        return entry, message

    # ------------------------------------------------------------------ 窗口聚合记录
    def record_window_result(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """别的页面产生的实时指标结果同步写入指标窗口聚合记录集。"""
        metric = store.find(MODULE, entry_id)
        if metric is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if metric.get("status") != "已上线":
            return None, f"指标当前状态为「{metric.get('status')}」，只有已上线指标才能写入窗口聚合记录"
        try:
            agg_value = float(values.get("聚合值"))
        except (TypeError, ValueError):
            return None, "聚合值必须是数字，窗口聚合记录未写入"
        try:
            sample_count = int(values.get("样本数") or 1)
        except (TypeError, ValueError):
            return None, "样本数必须是整数，窗口聚合记录未写入"
        with _window_lock:
            rows = store.rows(WINDOW_MODULE)
            seq = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            record = {
                "id": seq,
                "记录编号": f"WIN-{seq:04d}",
                "指标ID": entry_id,
                "指标编号": metric.get("指标编号"),
                "窗口开始": str(values.get("窗口开始") or _now()),
                "窗口结束": str(values.get("窗口结束") or _now()),
                "聚合值": agg_value,
                "样本数": sample_count,
                "来源": str(values.get("来源") or "实时指标上报"),
                "写入时间": _now(),
            }
            rows.append(record)
        return record, f"窗口聚合记录 {record['记录编号']} 已写入指标窗口聚合记录集"

    def list_window_records(self, entry_id: int) -> list[dict[str, Any]]:
        metric = store.find(MODULE, entry_id)
        if metric is None:
            return []
        code = str(metric.get("指标编号") or "")
        return [row for row in store.rows(WINDOW_MODULE) if str(row.get("指标编号")) == code]
