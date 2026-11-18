"""冷热归档业务规则：消费快照和分区策略，执行冷数据归档。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "cold_archive"
LIST_FIELDS = ["策略编号", "目标表名", "冷数据阈值", "归档路径", "压缩算法", "归档格式", "保留天数", "恢复超时"]
REQUIRED_FIELDS = ["策略编号", "目标表名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃", "停用策略": "已停用"}
NEGATIVE_ACTIONS = []


class ColdArchiveService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归档策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于冷热归档可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"归档策略已{action}"

    def execute_archive(self, entry_id: int, snapshot_id: str | None = None) -> dict[str, Any]:
        """执行归档：读取快照数据或目标表数据，写入归档记录。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "归档策略不存在"}
        table = entry.get("目标表名", "")
        rows: list[dict[str, Any]] = []
        if snapshot_id:
            snap = store.get_snapshot_data(snapshot_id)
            if snap:
                rows = snap.get("rows", [])
        if not rows:
            rows = store.table_rows(table)
        if not rows:
            return {"ok": False, "message": f"目标表 {table} 无数据可归档"}
        archive_record = {
            "archive_id": f"ARC-{entry_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "table": table,
            "row_count": len(rows),
            "path": entry.get("归档路径", f"/archive/{table}"),
            "format": entry.get("归档格式", "PARQUET"),
            "compressed_by": entry.get("压缩算法", "ZSTD"),
            "archived_at": datetime.now().isoformat(),
        }
        store.add_archive_record(archive_record)
        return {"ok": True, "message": f"已归档{len(rows)}行", "record": archive_record}
