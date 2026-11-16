"""监控大盘接口：维护大盘配置，覆盖提交大盘评审、发布大盘、下线大盘等动作，
以及监控大盘指标快照的生成与追溯。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/api/dashboard", tags=["监控大盘"])

service = DashboardService()

LIST_FIELDS = ["大盘编号", "大盘名称", "包含指标组", "布局模板", "刷新频率秒", "对比周期", "分享范围", "负责人"]
STATUSES = ["草稿", "大盘评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按大盘编号检索"),
    status: str | None = Query(default=None, description="草稿、大盘评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按大盘编号与状态过滤监控大盘列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出监控大盘清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dashboard", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条大盘配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"大盘配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条大盘配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="大盘配置已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """保存大盘配置：全字段落库；已发布大盘改配置会标记与上一发布快照的冲突，
    但大盘列表与详情仍引用同一快照。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条大盘配置执行提交大盘评审、发布大盘、下线大盘；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action_name())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/snapshot", response_model=ActionResult)
def refresh_snapshot(entry_id: int) -> ActionResult:
    """生成监控大盘指标快照：读取大盘配置与窗口聚合指标，聚合实时指标结果；
    上一次刷新中断时，从最后一个完整阶段继续。"""
    snapshot, message = service.refresh_snapshot(entry_id)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/{entry_id}/snapshot", response_model=dict)
def get_snapshot(entry_id: int) -> dict:
    """读取大盘最新指标快照；快照编号与大盘列表、详情里的当前快照编号一致。"""
    snapshot = service.get_snapshot(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"大盘配置 {entry_id} 还没有生成过指标快照")
    return snapshot


@router.get("/{entry_id}/snapshots")
def list_snapshots(entry_id: int) -> dict[str, Any]:
    """刷新批次历史：每一次刷新（含中断与恢复）都可追溯。"""
    snapshots = service.list_snapshots(entry_id)
    return {"module": "dashsnapshot", "total": len(snapshots), "items": snapshots}


@router.get("/{entry_id}/anomalies")
def list_anomalies(entry_id: int) -> dict[str, Any]:
    """异常卡片列表：卡片挂在刷新批次上，白屏原因可解释、可追溯。"""
    cards = service.list_anomalies(entry_id)
    return {"module": "anomalycard", "total": len(cards), "items": cards}
