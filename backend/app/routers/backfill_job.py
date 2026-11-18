"""补数调度接口：作业时间线、批量动作与补数分区结果集回查。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.backfill_job import BackfillJobService

router = APIRouter(prefix="/api/backfill_job", tags=["补数调度"])
service = BackfillJobService()
LIST_FIELDS = ["任务编号", "目标表名", "起始日期", "截止日期", "并行度", "覆盖策略", "完成水位", "失败区间"]
STATUSES = ["草稿", "运行中", "已完成", "已取消"]


class BatchActionPayload(BaseModel):
    """整组补数任务一次提交，逐条给出处理结果。"""

    job_ids: list[int] = Field(default_factory=list)
    action: str


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=backfill_job.csv"})


@router.get("/results")
def list_results() -> dict[str, Any]:
    """补数分区结果集列表：每张目标表保留一个完整版本。"""
    items = service.list_results()
    return {"items": items, "total": len(items)}


@router.get("/results/{table_name}")
def get_result(table_name: str) -> dict[str, Any]:
    """结果集详情：完成分区、失败分区、完成水位、边界冲突与合并视图。"""
    result = service.get_result(table_name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"表 {table_name} 暂无补数分区结果集")
    return result


@router.get("/{entry_id}/result")
def get_job_result(entry_id: int) -> dict[str, Any]:
    """沿数据流从补数任务回查其补数分区结果集。"""
    result = service.get_result_by_job(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 尚未产生补数分区结果集")
    return result


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="补数任务已登记", entry=entry_or_none)


@router.post("/batch-actions")
def run_batch_actions(payload: BatchActionPayload) -> dict[str, Any]:
    """一次提交整组补数任务，批量多选后逐条给出结果。"""
    if not payload.job_ids:
        raise HTTPException(status_code=400, detail="请至少选择一条补数任务")
    results = service.run_batch_actions(payload.job_ids, payload.action)
    ok_count = sum(1 for item in results if item["ok"])
    return {
        "ok": ok_count == len(results),
        "message": f"批量{payload.action}完成：成功 {ok_count}/{len(results)} 条",
        "items": results,
    }


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
