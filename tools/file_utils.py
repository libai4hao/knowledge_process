from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, TypeVar, overload, Optional

# 定义泛型，增加 load_json 的类型推断能力
T = TypeVar("T")

# 配置简单的日志，方便调试
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def ensure_dir(path: Path | str) -> Path:
    """
    确保目录存在，如果不存在则创建。
    
    :param path: 目标路径对象或字符串
    :return: 转化为 Path 对象的路径
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_text(content: str) -> str:
    """
    计算文本内容的 SHA-256 哈希值。

    :param content: 输入字符串
    :return: 64位十六进制哈希字符串
    """
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def safe_filename(name: str, fallback: str = "untitled", max_length: int = 255) -> str:
    """
    将字符串转换为合法的文件名，移除操作系统禁用的特殊字符。

    :param name: 原始文件名
    :param fallback: 如果清理后为空，使用的备用名
    :param max_length: 文件名最大长度限制（默认针对大多数文件系统）
    :return: 清理后的安全文件名
    """
    # 移除非打印字符和非法字符
    banned = r'<>:"/\|?*'
    # 替换非法字符为下划线，并去除首尾空格
    sanitized = "".join("_" if ch in banned else ch for ch in name).strip()

    # 处理 Windows 保留文件名
    reserved_names = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}
    if sanitized.upper() in reserved_names:
        sanitized = f"_{sanitized}"
    # 如果结果为空或全点号，使用备用名
    if not sanitized or sanitized.startswith('.'):
        result = fallback
    else:
        result = sanitized
    # 截断过长的文件名 (通常限制在 255 字节)
    return result[:max_length]


@overload
def load_json(path: Path, default: T) -> T: ...


@overload
def load_json(path: Path, default: None = None) -> Any | None: ...


def load_json(path: Path | str, default: Optional[Any] = None) -> Any:
    """
    从 JSON 文件加载数据。如果文件不存在或解析失败，返回默认值。

    :param path: 文件路径
    :param default: 失败时的返回值
    :return: 解析后的 Python 对象
    """
    path_obj = Path(path)
    if not path_obj.exists():
        return default

    try:
        with path_obj.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"无法解析 JSON 文件 {path_obj}: {e}")
        return default

def dump_json(path: Path | str, payload: Any, indent: int = 2) -> None:
    """
    将对象持久化原子性地写入 JSON 文件，自动创建父级目录。
    先写到临时文件再重命名，防止程序中途崩溃导致原文件数据丢失。
    :param path: 写入路径
    :param payload: 要序列化的对象
    :param indent: 缩进空格数
    :return: 写入是否成功
    """
    path_obj = Path(path)
    try:
        # 确保父目录存在
        path_obj.parent.mkdir(parents=True, exist_ok=True)

        with path_obj.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=indent)
        return True
    except (TypeError, OSError) as e:
        logger.error(f"写入 JSON 到 {path_obj} 失败: {e}")
        return False