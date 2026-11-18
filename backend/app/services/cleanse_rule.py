"""清洗策略业务规则：支持配置清洗规则并应用到目标数据集，驱动DWD表生成。"""
from __future__ import annotations

import re
from typing import Any

from app.store import store

MODULE = "cleanse_rule"
LIST_FIELDS = ["策略编号", "目标字段", "空值处理", "异常阈值", "正则表达式", "替换值", "策略版本", "执行顺序"]
REQUIRED_FIELDS = ["策略编号", "目标字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用", "停用策略": "已停用"}
NEGATIVE_ACTIONS = ["停用规则", "停用策略"]


class CleanseRuleService:
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
        if values.get("目标数据集"):
            entry["目标数据集"] = values["目标数据集"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于清洗策略可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 版本化：变更时旧版本保留
        store.save_version(MODULE, entry_id, entry)
        # 当策略生效/变更时，标记关联DWD表需要刷新
        if action in ("标记生效", "提交校验"):
            self._mark_dwd_refresh(entry)
        return entry, f"清洗策略已{action}"

    def _mark_dwd_refresh(self, entry: dict[str, Any]) -> None:
        dataset = entry.get("目标数据集", "")
        strategy_no = entry.get("策略编号", "")
        for row in store.rows("dwd_layer"):
            # 匹配父层ODS表名或清洗策略编号
            if row.get("清洗策略") == strategy_no:
                row["待刷新"] = True
                row["刷新原因"] = f"清洗策略 {strategy_no} 变更"
                store.save_version("dwd_layer", row["id"], row)
            else:
                # 通过ODS表名反向查找DWD
                parent = row.get("父层来源", "")
                ods = store.find_by_field("ods_layer", "表编号", parent)
                if ods and ods.get("表名称") == dataset:
                    row["待刷新"] = True
                    row["刷新原因"] = f"清洗策略 {strategy_no} 变更"
                    store.save_version("dwd_layer", row["id"], row)

    def update_cleanse_version(self, entry_id: int, new_values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """更新清洗策略并升级版本号，旧版本保留在版本历史中。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗策略 {entry_id} 不存在"
        old_version = entry.get("策略版本", "v1")
        # 升级版本号
        try:
            vnum = int(old_version.replace("v", ""))
            entry["策略版本"] = f"v{vnum + 1}"
        except Exception:
            entry["策略版本"] = "v2"
        for k in ["目标字段", "空值处理", "异常阈值", "正则表达式", "替换值", "执行顺序"]:
            if k in new_values:
                entry[k] = new_values[k]
        entry["pending"] = True
        store.save_version(MODULE, entry_id, entry)
        self._mark_dwd_refresh(entry)
        return entry, f"清洗策略已升级至 {entry['策略版本']}"

    def apply_cleanse(self, entry_id: int, data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对输入数据行应用清洗规则。"""
        entry = self.get_entry(entry_id)
        if not entry:
            return data_rows
        target_field = entry.get("目标字段", "")
        regex_str = entry.get("正则表达式", "")
        replace_val = entry.get("替换值", "")
        null_handle = entry.get("空值处理", "")
        result = []
        pattern = re.compile(regex_str) if regex_str else None
        for row in data_rows:
            new_row = dict(row)
            val = new_row.get(target_field)
            if val is None or val == "":
                if "填充" in null_handle:
                    if "填充为" in null_handle:
                        new_row[target_field] = null_handle.replace("填充为", "")
                    else:
                        new_row[target_field] = replace_val or "0"
            elif pattern and not pattern.search(str(val)):
                new_row[target_field] = replace_val
            result.append(new_row)
        return result

    def get_rules_for_dataset(self, dataset: str) -> list[dict[str, Any]]:
        return store.find_all_by_field(MODULE, "目标数据集", dataset)
