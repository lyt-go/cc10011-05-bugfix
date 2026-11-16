"""监控大盘业务规则：状态流转、字段校验、指标组引用同步与快照生成都收在这里。

修复要点（白屏与引用残留的共同根因）：
- 登记/保存时全字段落库，刷新频率、对比周期、分享范围、负责人不再丢失；
- 状态只能沿 草稿 → 大盘评审中 → 已发布 → 已下线 逐段推进，不能跳段、不能倒回；
- 发布与指标组写回在同一把锁内原子完成，并发发布只产生一个终态；
- 已下线/不存在的指标会从指标组剔除并留下可解释的异常卡片，刷新不再白屏；
- 快照按完整阶段推进，中断后从最后一个完整阶段继续，不回退到半成品。
"""
from __future__ import annotations

import copy
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.store import store

MODULE = "dashboard"
METRIC_MODULE = "realtimemetric"
WINDOW_MODULE = "metricwindow"    # 指标窗口聚合记录集
SNAPSHOT_MODULE = "dashsnapshot"  # 监控大盘指标快照
CARD_MODULE = "anomalycard"       # 异常卡片

REQUIRED_FIELDS = ["大盘编号", "大盘名称", "包含指标组"]
ALL_FIELDS = ["大盘编号", "大盘名称", "包含指标组", "布局模板", "刷新频率秒", "对比周期", "分享范围", "负责人"]
STATUS_ORDER = ["草稿", "大盘评审中", "已发布", "已下线"]
# 状态机：动作 -> (允许的前置状态, 目标状态)。生命周期只能逐段前进。
TRANSITIONS = {
    "提交大盘评审": ("草稿", "大盘评审中"),
    "发布大盘": ("大盘评审中", "已发布"),
    "下线大盘": ("已发布", "已下线"),
}
ACTION_RULES = {action: target for action, (_, target) in TRANSITIONS.items()}
NEGATIVE_ACTIONS = ["下线大盘"]

# 快照生成只能按完整阶段推进；中断恢复从最后一个完整阶段继续。
SNAPSHOT_STAGES = ["读取大盘配置", "聚合窗口指标", "生成指标快照", "回写快照引用"]

# 每个大盘一把锁：两个操作人同时发布同一大盘时只产生一个终态。
_locks_guard = threading.Lock()
_entry_locks: dict[int, threading.Lock] = {}

# 记录集取号器：快照/异常卡片的编号全局唯一，未落库的半成品也先占号。
_seq_guard = threading.Lock()
_seqs: dict[str, int] = {}


def _next_seq(module: str) -> int:
    with _seq_guard:
        current = _seqs.get(module)
        if current is None:
            current = max((int(row.get("id", 0)) for row in store.rows(module)), default=0)
        current += 1
        _seqs[module] = current
        return current


def _lock_for(entry_id: int) -> threading.Lock:
    with _locks_guard:
        return _entry_locks.setdefault(entry_id, threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def split_metric_codes(raw: Any) -> list[str]:
    """把「包含指标组」解析成指标编号列表，兼容逗号/顿号/分号/空白分隔，去重保序。"""
    parts = [str(item) for item in raw] if isinstance(raw, list) else re.split(r"[,，、;；\s]+", str(raw or ""))
    codes: list[str] = []
    for part in parts:
        code = part.strip()
        if code and code not in codes:
            codes.append(code)
    return codes


class DashboardService:
    # ------------------------------------------------------------------ 查询
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
            self._normalize(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("大盘编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            self._normalize(entry)
        return entry

    def _normalize(self, entry: dict[str, Any]) -> None:
        """补齐派生字段，保证大盘列表与详情引用同一份快照编号。"""
        entry.setdefault("配置版本", 1)
        entry.setdefault("当前快照编号", None)
        entry.setdefault("配置冲突", False)

    # ------------------------------------------------------------------ 登记与保存
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 全字段落库：刷新频率秒、对比周期、分享范围、负责人等不再在登记后丢失。
        entry.update({field: values.get(field) for field in ALL_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["配置版本"] = 1
        entry["当前快照编号"] = None
        entry["配置冲突"] = False
        rows.append(entry)
        self.sync_metric_refs()
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """保存大盘配置：全字段可改；已发布大盘改配置会标记与上一发布快照的冲突，
        但当前快照编号保持不变，大盘列表与详情仍引用同一快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"大盘配置 {entry_id} 不存在或已归档"
        self._normalize(entry)
        if entry.get("status") == STATUS_ORDER[-1]:
            return None, "大盘已下线，生命周期已结束，不能再修改配置"
        with _lock_for(entry_id):
            backup = copy.deepcopy(entry)
            try:
                changed = [field for field in ALL_FIELDS if field in values and values[field] is not None
                           and str(values[field]) != str(entry.get(field) or "")]
                if not changed:
                    return entry, "大盘配置无变化"
                for field in changed:
                    entry[field] = values[field]
                note = ""
                if "包含指标组" in changed:
                    active, retired = self._partition_metrics(split_metric_codes(entry.get("包含指标组")))
                    if retired:
                        # 指标组引用同步：保存时剔除已下线/不存在的指标，不留残留。
                        entry["包含指标组"] = ",".join(active)
                        note = f"；已同步剔除下线指标：{'、'.join(retired)}"
                if entry.get("status") == "已发布":
                    entry["配置版本"] = int(entry.get("配置版本", 1)) + 1
                    entry["配置冲突"] = True  # 与上一发布快照冲突；列表与详情仍引用同一快照
                self.sync_metric_refs()
            except Exception as exc:  # 保存失败整体回滚，不留写了一半的配置
                entry.clear()
                entry.update(backup)
                return None, f"保存大盘配置失败：{exc}；已回滚到修改前"
        return entry, f"大盘配置已保存（变更字段：{'、'.join(changed)}）{note}"

    # ------------------------------------------------------------------ 状态流转
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"大盘配置 {entry_id} 不存在或已归档"
        self._normalize(entry)
        if action not in TRANSITIONS:
            return None, f"动作「{action}」不属于监控大盘可执行范围"
        source, target = TRANSITIONS[action]
        with _lock_for(entry_id):
            current = str(entry.get("status") or "")
            if current == target:
                return None, f"大盘配置已处于「{target}」，请勿重复{action}"
            if current != source:
                return None, f"大盘配置当前状态为「{current}」，须处于「{source}」才能{action}；生命周期不能跳段或倒回"
            if action == "发布大盘":
                return self._publish_locked(entry)
            if action == "下线大盘":
                return self._offline_locked(entry)
            entry["status"] = target
            entry["pending"] = True
            entry["abnormal"] = False
            self.sync_metric_refs()
            return entry, "大盘配置已提交大盘评审"

    def _publish_locked(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """发布大盘：指标组解析、快照生成、状态推进在同一把锁内原子完成。
        任一步失败整体回滚，绝不出现「大盘已发布而指标组仍是旧列表」的分离结果。"""
        backup = copy.deepcopy(entry)
        snapshot: dict[str, Any] | None = None
        try:
            codes = split_metric_codes(entry.get("包含指标组"))
            active, retired = self._partition_metrics(codes)
            if not active:
                return None, "指标组内没有可用指标（全部已下线或不存在），请先修正指标组再发布"
            entry["包含指标组"] = ",".join(active)
            snapshot = self._new_snapshot(entry)
            store.rows(SNAPSHOT_MODULE).append(snapshot)
            self._advance_snapshot(entry, snapshot)
            for code in retired:
                card = self._new_card(entry, snapshot, code, "指标已下线",
                                      f"发布时指标 {code} 已下线或不存在，已从指标组剔除")
                snapshot["异常卡片"].append(card)
            self._flush_cards(snapshot)
            entry["status"] = "已发布"
            entry["pending"] = False
            entry["abnormal"] = bool(snapshot["异常卡片"])
            self.sync_metric_refs()
        except Exception as exc:
            entry.clear()
            entry.update(backup)
            if snapshot is not None and snapshot in store.rows(SNAPSHOT_MODULE):
                store.rows(SNAPSHOT_MODULE).remove(snapshot)
            self.sync_metric_refs()
            return None, f"发布大盘失败：{exc}；已整体回滚，大盘状态与指标组保持原状"
        message = "大盘配置已发布大盘"
        if retired:
            message += f"，已剔除下线指标：{'、'.join(retired)}（详见异常卡片）"
        return entry, message

    def _offline_locked(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        backup = copy.deepcopy(entry)
        try:
            entry["status"] = "已下线"
            entry["pending"] = False
            entry["abnormal"] = True
            # 保留当前快照编号：历史刷新批次与异常卡片仍可追溯。
            self.sync_metric_refs()  # 别的入口的指标组引用随状态变化同步更新
        except Exception as exc:
            entry.clear()
            entry.update(backup)
            return None, f"下线大盘失败：{exc}；已回滚"
        return entry, "大盘配置已下线大盘，关联指标组引用已同步更新"

    # ------------------------------------------------------------------ 指标组引用同步
    def _partition_metrics(self, codes: list[str]) -> tuple[list[str], list[str]]:
        metrics = {str(metric.get("指标编号")): metric for metric in store.rows(METRIC_MODULE)}
        active = [code for code in codes if code in metrics and metrics[code].get("status") != "已下线"]
        retired = [code for code in codes if code not in active]
        return active, retired

    def sync_metric_refs(self) -> None:
        """维护指标 → 大盘的反向引用：大盘状态或指标组变化后，别的入口看到的指标组同步更新。"""
        mapping: dict[str, list[str]] = {}
        for entry in store.rows(MODULE):
            if entry.get("status") == "已下线":
                continue
            for code in split_metric_codes(entry.get("包含指标组")):
                mapping.setdefault(code, []).append(str(entry.get("大盘编号") or ""))
        for metric in store.rows(METRIC_MODULE):
            metric["关联大盘"] = mapping.get(str(metric.get("指标编号") or ""), [])

    def sync_metric_offline(self, metric_code: str) -> list[str]:
        """指标下线后同步清理所有大盘的指标组引用，并为已发布大盘留下可解释的异常卡片。"""
        affected: list[str] = []
        if not metric_code:
            return affected
        for entry in store.rows(MODULE):
            if metric_code not in split_metric_codes(entry.get("包含指标组")):
                continue
            with _lock_for(int(entry.get("id", 0))):
                self._normalize(entry)
                codes = split_metric_codes(entry.get("包含指标组"))
                if metric_code not in codes:
                    continue
                backup = copy.deepcopy(entry)
                try:
                    entry["包含指标组"] = ",".join(code for code in codes if code != metric_code)
                    if entry.get("status") == "已发布":
                        entry["配置版本"] = int(entry.get("配置版本", 1)) + 1
                        entry["配置冲突"] = True
                        entry["abnormal"] = True
                        card = self._new_card(entry, None, metric_code, "指标已下线",
                                              f"指标 {metric_code} 已下线，已同步从大盘指标组剔除；"
                                              f"下次刷新快照将按新指标组聚合")
                        store.rows(CARD_MODULE).append(card)
                except Exception:
                    entry.clear()
                    entry.update(backup)
                    raise
                affected.append(str(entry.get("大盘编号") or ""))
        self.sync_metric_refs()
        return affected

    # ------------------------------------------------------------------ 快照生成
    def refresh_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """生成监控大盘指标快照：读取大盘配置与窗口聚合指标，聚合实时指标结果。
        上一次刷新中断时，从最后一个完整阶段继续，不回退到半成品。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"大盘配置 {entry_id} 不存在或已归档"
        self._normalize(entry)
        if entry.get("status") != "已发布":
            return None, f"只有已发布的大盘才能刷新快照，当前状态为「{entry.get('status')}」"
        with _lock_for(entry_id):
            snapshots = [row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("大盘ID", 0)) == entry_id]
            latest = snapshots[-1] if snapshots else None
            if latest is not None and latest.get("状态") == "中断":
                snapshot = latest  # 中断恢复：沿用原刷新批次，从最后一个完整阶段继续
            else:
                snapshot = self._new_snapshot(entry)
                store.rows(SNAPSHOT_MODULE).append(snapshot)
            backup = copy.deepcopy(entry)
            try:
                self._advance_snapshot(entry, snapshot)
                self._flush_cards(snapshot)
            except Exception as exc:
                entry.clear()
                entry.update(backup)
                snapshot["状态"] = "中断"
                snapshot["中断原因"] = str(exc)
                return None, (f"快照生成中断于阶段「{snapshot.get('阶段') or '起始'}」：{exc}；"
                              f"已完成 {len(snapshot.get('已完成阶段') or [])} 个阶段，再次刷新将从断点继续")
            return snapshot, f"监控大盘指标快照已生成（刷新批次 {snapshot['刷新批次']}）"

    def get_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        snapshots = [row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("大盘ID", 0)) == entry_id]
        return snapshots[-1] if snapshots else None

    def list_snapshots(self, entry_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(SNAPSHOT_MODULE) if int(row.get("大盘ID", 0)) == entry_id]

    def list_anomalies(self, entry_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(CARD_MODULE) if int(row.get("大盘ID", 0)) == entry_id]

    def _new_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        seq = _next_seq(SNAPSHOT_MODULE)
        batch_seq = max((int(row.get("批次序号", 0)) for row in store.rows(SNAPSHOT_MODULE)
                         if int(row.get("大盘ID", 0)) == int(entry.get("id", 0))), default=0) + 1
        return {
            "id": seq,
            "快照编号": f"SNAP-{seq:04d}",
            "刷新批次": f"{entry.get('大盘编号')}-BATCH-{batch_seq:03d}",
            "批次序号": batch_seq,
            "大盘ID": entry.get("id"),
            "大盘编号": entry.get("大盘编号"),
            "配置版本": entry.get("配置版本", 1),
            "状态": "进行中",
            "阶段": None,
            "已完成阶段": [],
            "大盘配置": {},
            "窗口聚合": [],
            "指标结果": [],
            "异常卡片": [],
            "卡片已落库": False,
            "创建时间": _now(),
            "完成时间": None,
        }

    def _advance_snapshot(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """按完整阶段推进快照；已完成阶段连续校验，损坏时只保留完整前缀。"""
        runners: dict[str, Callable[[dict[str, Any], dict[str, Any]], None]] = {
            "读取大盘配置": self._stage_read_config,
            "聚合窗口指标": self._stage_aggregate_windows,
            "生成指标快照": self._stage_build_results,
            "回写快照引用": self._stage_writeback_ref,
        }
        done = [stage for stage in (snapshot.get("已完成阶段") or []) if stage in SNAPSHOT_STAGES]
        prefix = 0
        for stage in done:
            if stage == SNAPSHOT_STAGES[prefix]:
                prefix += 1
            else:
                break
        snapshot["已完成阶段"] = SNAPSHOT_STAGES[:prefix]
        for stage in SNAPSHOT_STAGES[prefix:]:
            runners[stage](entry, snapshot)
            snapshot["已完成阶段"].append(stage)
            snapshot["阶段"] = stage
        snapshot["状态"] = "完成"
        snapshot["完成时间"] = _now()

    def _stage_read_config(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """阶段一：读取大盘配置。"""
        snapshot["大盘配置"] = {field: entry.get(field) for field in ALL_FIELDS}
        snapshot["配置版本"] = entry.get("配置版本", 1)

    def _stage_aggregate_windows(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """阶段二：读取监控大盘接入的窗口聚合指标，聚合实时指标结果。"""
        metrics = {str(metric.get("指标编号")): metric for metric in store.rows(METRIC_MODULE)}
        windows = store.rows(WINDOW_MODULE)
        aggregated: list[dict[str, Any]] = []
        anomalies: list[dict[str, str]] = []
        for code in split_metric_codes(entry.get("包含指标组")):
            metric = metrics.get(code)
            if metric is None:
                anomalies.append({"指标编号": code, "异常类型": "指标不存在",
                                  "说明": f"指标 {code} 不存在，已从本次聚合剔除"})
                continue
            if metric.get("status") == "已下线":
                anomalies.append({"指标编号": code, "异常类型": "指标已下线",
                                  "说明": f"指标 {code} 已下线，已从本次聚合剔除"})
                continue
            records = [row for row in windows if str(row.get("指标编号")) == code]
            if not records:
                # 数据缺口显式落成异常卡片，刷新结果不再是无解释的白屏。
                anomalies.append({"指标编号": code, "异常类型": "窗口聚合数据缺失",
                                  "说明": f"指标 {code} 在指标窗口聚合记录集中没有数据，"
                                          f"请确认实时指标是否已上报窗口结果"})
                continue
            values = [float(row.get("聚合值") or 0) for row in records]
            func = str(metric.get("聚合函数") or "AVG").upper()
            if func == "SUM":
                agg_value = sum(values)
            elif func == "MAX":
                agg_value = max(values)
            elif func == "MIN":
                agg_value = min(values)
            elif func == "COUNT":
                agg_value = float(len(values))
            else:
                agg_value = sum(values) / len(values)
            latest = records[-1]
            aggregated.append({
                "指标编号": code,
                "指标名称": metric.get("指标名称"),
                "指标状态": metric.get("status"),
                "聚合函数": func,
                "窗口记录数": len(records),
                "聚合值": round(agg_value, 4),
                "最新值": float(latest.get("聚合值") or 0),
                "最大值": max(values),
                "最小值": min(values),
                "最近窗口结束": latest.get("窗口结束"),
            })
        snapshot["窗口聚合"] = aggregated
        snapshot["阶段异常"] = anomalies

    def _stage_build_results(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """阶段三：生成监控大盘指标快照，异常卡片挂上刷新批次以便追溯。"""
        snapshot["指标结果"] = list(snapshot.get("窗口聚合") or [])
        cards = [self._new_card(entry, snapshot, item["指标编号"], item["异常类型"], item["说明"])
                 for item in (snapshot.get("阶段异常") or [])]
        snapshot["异常卡片"] = list(snapshot.get("异常卡片") or []) + cards

    def _stage_writeback_ref(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """阶段四：把快照引用回写大盘配置，列表与详情自此引用同一快照。"""
        entry["当前快照编号"] = snapshot["快照编号"]
        entry["配置冲突"] = False
        entry["abnormal"] = bool(snapshot.get("异常卡片"))

    def _flush_cards(self, snapshot: dict[str, Any]) -> None:
        """快照完整落成才把异常卡片写入记录集，半成品的卡片不入库。"""
        if snapshot.get("卡片已落库"):
            return
        rows = store.rows(CARD_MODULE)
        for card in snapshot.get("异常卡片") or []:
            if card not in rows:
                rows.append(card)
        snapshot["卡片已落库"] = True

    def _new_card(self, entry: dict[str, Any], snapshot: dict[str, Any] | None,
                  metric_code: str, kind: str, note: str) -> dict[str, Any]:
        # 取号器全局发号：同一批快照里未落库的卡片编号也唯一可追溯。
        seq = _next_seq(CARD_MODULE)
        return {
            "id": seq,
            "卡片编号": f"CARD-{seq:04d}",
            "大盘ID": entry.get("id"),
            "大盘编号": entry.get("大盘编号"),
            "快照编号": (snapshot or {}).get("快照编号") or entry.get("当前快照编号"),
            "刷新批次": (snapshot or {}).get("刷新批次") or "指标下线同步",
            "指标编号": metric_code,
            "异常类型": kind,
            "说明": note,
            "状态": "未处理",
            "生成时间": _now(),
        }
