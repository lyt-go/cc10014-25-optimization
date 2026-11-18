"""去重配置业务规则：支持按业务键去重并保留最新记录，驱动DWD表生成。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dedup_config"
LIST_FIELDS = ["配置编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值", "配置版本", "目标数据集"]
REQUIRED_FIELDS = ["配置编号", "业务键列"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃", "停用配置": "已停用"}
NEGATIVE_ACTIONS = []


class DedupConfigService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("配置编号", "")).casefold()]
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
            return None, f"去重配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于去重配置可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action in ("标记生效", "提交校验"):
            self._mark_dwd_refresh(entry)
        return entry, f"去重配置已{action}"

    def _mark_dwd_refresh(self, entry: dict[str, Any]) -> None:
        dataset = entry.get("目标数据集", "")
        config_no = entry.get("配置编号", "")
        for row in store.rows("dwd_layer"):
            if row.get("去重配置") == config_no:
                row["待刷新"] = True
                row["刷新原因"] = f"去重配置 {config_no} 变更"
                store.save_version("dwd_layer", row["id"], row)
            else:
                parent = row.get("父层来源", "")
                ods = store.find_by_field("ods_layer", "表编号", parent)
                if ods and ods.get("表名称") == dataset:
                    row["待刷新"] = True
                    row["刷新原因"] = f"去重配置 {config_no} 变更"
                    store.save_version("dwd_layer", row["id"], row)

    def update_dedup_version(self, entry_id: int, new_values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """更新去重配置并升级版本号。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"去重配置 {entry_id} 不存在"
        old_version = entry.get("配置版本", "v1")
        try:
            vnum = int(old_version.replace("v", ""))
            entry["配置版本"] = f"v{vnum + 1}"
        except Exception:
            entry["配置版本"] = "v2"
        for k in ["业务键列", "排序字段", "保留策略", "时间窗口", "相似度阈值"]:
            if k in new_values:
                entry[k] = new_values[k]
        entry["pending"] = True
        store.save_version(MODULE, entry_id, entry)
        self._mark_dwd_refresh(entry)
        return entry, f"去重配置已升级至 {entry['配置版本']}"

    def apply_dedup(self, entry_id: int, data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按业务键去重，保留最新（按数据中 _sort 字段或默认取最后一条）。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return data_rows
        key_col = entry.get("业务键列", "")
        if not key_col:
            return data_rows
        seen: dict[str, dict[str, Any]] = {}
        for row in data_rows:
            key_val = str(row.get(key_col, ""))
            if key_val:
                seen[key_val] = row
        return list(seen.values())

    def get_configs_for_dataset(self, dataset: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "目标数据集", dataset)
