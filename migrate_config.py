#!/usr/bin/env python3
"""
migrate_config.py —— 把旧的单目标配置转换成新的多目标格式。

旧格式（规则直接写在顶层，只有一个 target_app）：
    { "target_app": {...}, "allow_patterns": [...], ... }

新格式（共用规则放 defaults，targets 里列多个应用）：
    { "defaults": { "allow_patterns": [...] }, "targets": [ {...}, {...} ] }

转换会保留你调过的所有规则，原文件备份成 config.json.bak。
已经是新格式则什么都不做。

用法： python3 migrate_config.py [配置文件路径]
"""

from __future__ import annotations

import json
import os
import shutil
import sys

# 这些是每个目标各自的身份信息，不属于共用规则
IDENTITY_KEYS = {"target_app", "id", "label", "bundle_ids", "name_contains", "enabled"}
# 这些是全局项，留在顶层
GLOBAL_KEYS = {"poll_interval", "kill_switch_file", "log_file", "cdp_port"}

EXTRA_TARGETS = [
    {
        "id": "cursor",
        "label": "Cursor",
        "bundle_ids": ["com.todesktop.230313mzl4w4u92"],
        "name_contains": ["cursor"],
        "enabled": False,
    },
    {
        "id": "vscode",
        "label": "VS Code",
        "bundle_ids": ["com.microsoft.VSCode"],
        "name_contains": ["code"],
        "enabled": False,
    },
]


# 命令审批弹窗（"Your approval is required to continue: <命令>"）需要的规则。
# 升级时只补缺失项，不删、不改用户已有的规则。
COMMAND_APPROVAL_MARKERS = [
    "your approval is required",
    "approval is required to continue",
]
COMMAND_APPROVAL_BUTTON_DENY = ["deny"]  # 同时覆盖 "Deny" 和 "Always deny"

# 弹窗里要执行的命令命中这些规则时，不自动点 Allow，留给用户手动处理。
# 原则：宁可误拦，不可误放。
COMMAND_DENY_PATTERNS = [
    r"\bsudo\b",
    r"\bgit\s+push\b.*(\s-f\b|--force)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bgit\s+branch\s+.*-D\b",
    r"--no-verify\b",
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b",  # 下载即执行
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/(sd|disk|nvme)",
    r"\bchmod\s+-R\s+777\b",
    r"\bfind\b.*\s-delete\b",
    r"\b(shutdown|reboot|halt)\b",
    r"\bkillall\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\b(npm|yarn|pnpm)\s+publish\b",
    r"\bdocker\s+(system\s+prune|rm|rmi|volume\s+rm)\b",
    r"\bkubectl\s+delete\b",
    r"\bterraform\s+(apply|destroy)\b",
    r"\baws\s+\S+\s+(delete|rm|terminate)",
]


# 命中这些的命令照常放行，但会记录到 ~/Library/Logs/auto_confirm_risky.log
COMMAND_WARN_PATTERNS = [
    r"\brm\s+-[a-z]*[rf]",                      # rm -rf / rm -r / rm -f
    r"\brm\s+.*--(recursive|force)\b",
]


def upgrade_rules(raw: dict) -> list[str]:
    """给多目标配置的 defaults 补上命令审批相关规则，返回做了哪些改动。"""
    defaults = raw.setdefault("defaults", {})
    changes: list[str] = []

    def merge(key: str, items: list[str]) -> None:
        current = defaults.setdefault(key, [])
        added = [i for i in items if i not in current]
        if added:
            current.extend(added)
            changes.append(f"{key} +{len(added)}")

    merge("dialog_markers", COMMAND_APPROVAL_MARKERS)
    merge("deny_patterns", COMMAND_APPROVAL_BUTTON_DENY)
    merge("command_deny_patterns", COMMAND_DENY_PATTERNS)
    merge("command_warn_patterns", COMMAND_WARN_PATTERNS)
    return changes


def derive_label(app: dict) -> str:
    names = app.get("name_contains") or []
    if names:
        return str(names[0]).title()
    bundles = app.get("bundle_ids") or []
    if bundles:
        parts = [
            p for p in str(bundles[0]).split(".")
            if p.lower() not in {"com", "dev", "io", "org", "net",
                                 "desktop", "app", "macos", "mac"}
        ]
        return (parts[-1] if parts else bundles[0]).title()
    return "Target"


def migrate(path: str) -> int:
    if not os.path.exists(path):
        print(f"配置文件不存在，跳过迁移: {path}")
        return 0

    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if "targets" in raw:
        changes = upgrade_rules(raw)
        if not changes:
            print("已经是最新格式，无需迁移")
            return 0
        backup = path + ".bak"
        shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"已补充命令审批规则: {', '.join(changes)}")
        print(f"  原文件备份: {backup}")
        return 0

    app = raw.get("target_app", {})
    label = derive_label(app)

    defaults = {
        k: v for k, v in raw.items()
        if k not in IDENTITY_KEYS and k not in GLOBAL_KEYS and not k.startswith("_")
    }

    new = {
        "_comment": [
            "多目标配置（由 migrate_config.py 从旧格式自动转换）。",
            "defaults 是共用规则，targets 里每个条目可单独覆盖任意一项。",
            "想加应用：在 targets 里加一条，bundle_ids 用",
            "`python3 auto_confirm.py --list-apps` 查，enabled 设 true。",
        ],
    }
    for key in ("poll_interval", "kill_switch_file", "log_file", "cdp_port"):
        if key in raw:
            new[key] = raw[key]

    new["defaults"] = defaults
    upgrade_rules(new)

    primary = {
        "id": label.lower().replace(" ", "-"),
        "label": label,
        "bundle_ids": app.get("bundle_ids", []),
        "name_contains": app.get("name_contains", []),
        "enabled": True,
    }
    existing_ids = {primary["id"]}
    targets = [primary]
    for extra in EXTRA_TARGETS:
        if extra["id"] not in existing_ids:
            targets.append(extra)
    new["targets"] = targets

    backup = path + ".bak"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(new, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"已迁移为多目标格式: {path}")
    print(f"  原文件备份: {backup}")
    print(f"  主目标: {primary['label']} (id={primary['id']})")
    print(f"  另附 {len(targets) - 1} 个未启用的模板目标")
    return 0


if __name__ == "__main__":
    target_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.expanduser(
            "~/Library/Application Support/AutoConfirm/config.json"
        )
    )
    raise SystemExit(migrate(target_path))
