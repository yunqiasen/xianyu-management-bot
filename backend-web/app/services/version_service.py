"""
版本检测服务

功能：
1. 获取系统当前版本号（复用桌面启动器的版本号常量）
2. 向远程更新服务器请求 version.json，对比版本号判断是否有新版本
3. 返回统一的检测结果字典供路由层包装为 ApiResponse

说明：
- 更新服务器地址与格式跟桌面启动器（launcher/updater.py）保持一致，
  详见 data/update_config.json 与 https://xy-update.zhinianboke.com/version.json。
- 所有外部请求使用 httpx 异步客户端，超时 10 秒。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


# 默认更新服务器地址（与 launcher/updater.py 保持一致）
_DEFAULT_UPDATE_URL = "https://xy-update.zhinianboke.com"
# 远程 version.json 请求超时时间（秒）
_REMOTE_TIMEOUT_SECONDS = 10
# HTTP User-Agent，便于服务端识别来源
_USER_AGENT = "XianyuAutoReply-WebUpdater"


def get_current_version() -> str:
    from common.runtime_version import build_identity
    return build_identity()["version"]


def _get_update_url() -> str:
    """
    获取更新服务器基础 URL

    读取顺序：
    1. data/update_config.json 中的 update_url 字段
    2. 默认地址 _DEFAULT_UPDATE_URL

    Returns:
        基础 URL（不含尾部斜杠）
    """
    try:
        # 以项目根为基准（Docker 里 WORKDIR=/app，根目录下有 data/）
        config_path = Path.cwd() / "data" / "update_config.json"
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            url = str(data.get("update_url", "") or "").strip().rstrip("/")
            if url:
                return url
    except Exception as exc:
        logger.warning(f"读取更新服务器配置失败，使用默认地址: {exc}")
    return _DEFAULT_UPDATE_URL


def _compare_versions(local: str, remote: str) -> bool:
    """
    比较版本号，判断远程版本是否比本地新

    规则：按点分割后逐段比较整数值，忽略非数字前缀（如开头的 "v"）。

    Args:
        local: 本地版本号
        remote: 远程版本号

    Returns:
        True 表示远程更新需要升级；False 表示本地已是最新或无法比较
    """
    def _normalize(ver: str) -> list[int]:
        raw = (ver or "").strip().lstrip("vV")
        parts: list[int] = []
        for seg in raw.split("."):
            try:
                parts.append(int(seg))
            except ValueError:
                # 非数字段视为 0，避免抛异常
                parts.append(0)
        return parts

    try:
        local_parts = _normalize(local)
        remote_parts = _normalize(remote)
        # 补齐长度，短的补 0
        max_len = max(len(local_parts), len(remote_parts))
        local_parts += [0] * (max_len - len(local_parts))
        remote_parts += [0] * (max_len - len(remote_parts))
        return remote_parts > local_parts
    except Exception:
        return False


async def check_update() -> dict[str, Any]:
    """Fork releases are reviewed and built from source, never upstream binaries."""
    from common.runtime_version import build_identity
    identity = build_identity()
    return {
        "has_update": False,
        "current_version": identity["version"],
        "remote_version": "",
        "description": "增强版仅通过已审核源码发布；main 用于原版同步预览。",
        "filename": "", "download_url": "", "error": "",
        "repository": identity["repository"],
        "commit": identity["commit"], "update_mode": "source_review",
    }
