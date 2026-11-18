"""分区策略业务规则：管理表的分区策略，支持冷热分区判定。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "partition_mgr"
LIST_FIELDS = ["策略编号", "目标表名", "分区字段", "分区粒度", "保留天数", "冷热阈值", "预建数量", "目录规则"]
REQUIRED_FIELDS = ["策略编号", "目标表名"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃", "停用策略": "已停用"}
NEGATIVE_ACTIONS = []


class PartitionMgrService:
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
            return None, f"分区策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分区治理可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"分区策略已{action}"

    def get_cold_partitions(self, entry_id: int) -> list[dict[str, Any]]:
        """返回该策略下应归档的冷分区列表（模拟）。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return []
        threshold = int(entry.get("冷热阈值", "30") or "30")
        table = entry.get("目标表名", "")
        rows = store.table_rows(table)
        partitions: dict[str, int] = {}
        for row in rows:
            p = str(row.get("dt", ""))
            if p:
                partitions[p] = partitions.get(p, 0) + 1
        result = []
        for p, cnt in partitions.items():
            result.append({
                "partition": p,
                "row_count": cnt,
                "is_cold": True,  # 简化：全部视为可归档
                "threshold_days": threshold,
            })
        return result
