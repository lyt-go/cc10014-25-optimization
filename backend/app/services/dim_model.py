"""维度模型业务规则：管理维度表定义，支持SCD策略。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dim_model"
LIST_FIELDS = ["模型编号", "维度名称", "自然键列", "代理键列", "缓慢变化策略", "生效日期", "失效日期", "刷新周期"]
REQUIRED_FIELDS = ["模型编号", "维度名称"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃", "停用模型": "已停用"}
NEGATIVE_ACTIONS = []


class DimModelService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("模型编号", "")).casefold()]
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
            return None, f"维度模型 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维度建模可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"维度模型已{action}"

    def build_dim_from_dwd(self, entry_id: int) -> dict[str, Any]:
        """从关联DWD表数据生成维度表数据（模拟）。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "维度模型不存在"}
        dim_name = entry.get("维度名称", "")
        key_col = entry.get("自然键列", "")
        # 根据维度名称找对应的DWD表
        dwd_table = ""
        if "用户" in dim_name:
            dwd_table = "dwd_user_detail"
        elif "商品" in dim_name:
            dwd_table = "dwd_product_detail"
        elif "日期" in dim_name:
            dwd_table = "dwd_date_detail"
        dwd_data = store.table_rows(dwd_table)
        if not dwd_data:
            # 尝试构建
            from app.services.dwd_layer import DwdLayerService
            dwd_svc = DwdLayerService()
            for row in store.rows("dwd_layer"):
                if row.get("表名称") == dwd_table:
                    dwd_svc.build_dwd_from_ods(row["id"])
                    break
            dwd_data = store.table_rows(dwd_table)
        dim_rows = []
        seen = set()
        for row in dwd_data:
            key_val = str(row.get(key_col, ""))
            if key_val and key_val not in seen:
                seen.add(key_val)
                dim_row = dict(row)
                dim_row["_dim_key"] = key_val
                dim_rows.append(dim_row)
        dim_table = f"dim_{dim_name.replace('维度', '').strip()}"
        store.set_table_rows(dim_table, dim_rows)
        return {"ok": True, "message": f"维度表 {dim_table} 已生成，共{len(dim_rows)}行", "rows": dim_rows}
