"""明细层管理业务规则：消费ODS层数据，应用清洗+去重，生成DWD明细表，版本化管理。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.store import store

MODULE = "dwd_layer"
LIST_FIELDS = ["表编号", "父层来源", "表名称", "粒度说明", "分区字段", "关联键列", "刷新水位", "创建批次"]
REQUIRED_FIELDS = ["表编号", "父层来源", "表名称"]
STATUS_ORDER = ["草稿", "待校验", "已上线", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认上线": "已上线", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


class DwdLayerService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("表编号", "")).casefold()]
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
        if values.get("清洗策略"):
            entry["清洗策略"] = values["清洗策略"]
        if values.get("去重配置"):
            entry["去重配置"] = values["去重配置"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"明细表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于明细层管理可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"明细表已{action}"

    def build_dwd_from_ods(self, entry_id: int) -> dict[str, Any]:
        """从关联的ODS表读取数据，应用清洗+去重，写入DWD模拟表。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "明细表不存在"}
        # 找到父层ODS表
        parent_code = entry.get("父层来源", "")
        ods_entry = None
        for row in store.rows("ods_layer"):
            if row.get("表编号") == parent_code or row.get("表名称") == parent_code:
                ods_entry = row
                break
        if not ods_entry:
            return {"ok": False, "message": f"父层来源 {parent_code} 不存在"}
        ods_table = ods_entry.get("表名称", "")
        ods_data = store.table_rows(ods_table)
        if not ods_data:
            # 如果ODS没有数据，用seed数据生成模拟数据
            ods_data = self._mock_ods_data(ods_table)
            store.set_table_rows(ods_table, ods_data)
        # 应用清洗
        cleanse_id = entry.get("清洗策略", "")
        if cleanse_id:
            from app.services.cleanse_rule import CleanseRuleService
            cs = CleanseRuleService()
            c_entry = store.find("cleanse_rule", self._resolve_id("cleanse_rule", cleanse_id))
            if c_entry:
                ods_data = cs.apply_cleanse(c_entry["id"], ods_data)
        # 应用去重
        dedup_id = entry.get("去重配置", "")
        if dedup_id:
            from app.services.dedup_config import DedupConfigService
            ds = DedupConfigService()
            d_entry = store.find("dedup_config", self._resolve_id("dedup_config", dedup_id))
            if d_entry:
                ods_data = ds.apply_dedup(d_entry["id"], ods_data)
        # 写入DWD模拟表
        dwd_table = entry.get("表名称", "")
        store.set_table_rows(dwd_table, ods_data)
        entry["刷新水位"] = date.today().isoformat()
        store.save_version(MODULE, entry_id, entry)
        # 注册血缘
        store.add_lineage(ods_table, dwd_table, ods_entry.get("字段列表", ""),
                          transform_logic="清洗+去重", batch_id=entry.get("创建批次", ""))
        return {"ok": True, "message": f"DWD表 {dwd_table} 已生成，共{len(ods_data)}行"}

    def _resolve_id(self, module: str, code: str) -> int:
        for row in store.rows(module):
            if row.get("策略编号") == code or row.get("配置编号") == code:
                return int(row["id"])
        return 0

    def _mock_ods_data(self, table_name: str) -> list[dict[str, Any]]:
        if "order" in table_name:
            return [
                {"order_id": f"O{i:04d}", "order_amount": f"{i*10.5:.2f}", "order_status": "已完成" if i % 2 else "待支付", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "pay" in table_name:
            return [
                {"pay_id": f"P{i:04d}", "pay_amount": f"{i*8.3:.2f}", "pay_time": "2026-11-01T10:00:00", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        elif "user" in table_name:
            return [
                {"user_id": f"U{i:04d}", "register_time": "2026-01-01T00:00:00", "user_level": "普通" if i % 2 else "VIP", "dt": "2026-11-01"}
                for i in range(1, 6)
            ]
        return [{"_id": i, "dt": "2026-11-01"} for i in range(1, 6)]

    def get_table_data(self, table_name: str) -> list[dict[str, Any]]:
        return store.table_rows(table_name)

    def get_backfill_result(self, entry_id: int) -> dict[str, Any] | None:
        """明细层与补数调度跨模块联动：按DWD表回查补数分区结果集。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        return store.get_backfill_result(str(entry.get("表名称", "")))
