import os
import re
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"

_NOVEL_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,50}$')
_CONFIG_CACHE = None


def validate_novel_id(novel_id: str):
    """校验 novel_id，防止路径遍历和非法字符"""
    if not _NOVEL_ID_RE.match(novel_id):
        raise ValueError(f"无效的项目ID: '{novel_id}'，仅允许字母、数字、下划线、连字符，长度1-50")


def load_config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _CONFIG_CACHE = yaml.safe_load(f)
    # 环境变量覆盖敏感配置
    env_key = os.environ.get("NOVEL_API_KEY")
    if env_key and "api" in _CONFIG_CACHE:
        _CONFIG_CACHE["api"]["api_key"] = env_key
    return _CONFIG_CACHE


def reload_config() -> dict:
    """强制重新加载配置"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    return load_config()


def get_novel_dir(novel_id: str) -> Path:
    validate_novel_id(novel_id)
    d = DATA_DIR / f"novel_{novel_id}"
    # 安全检查：确保解析后的路径仍在 DATA_DIR 下
    resolved = d.resolve()
    if not str(resolved).startswith(str(DATA_DIR.resolve())):
        raise ValueError("非法路径")
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_novel_subdirs(novel_id: str) -> dict:
    base = get_novel_dir(novel_id)
    subdirs = {
        "base": base,
        "core_prompt": base / "core_prompt",
        "style_samples": base / "style_samples",
        "plans": base / "plans",
        "chapters": base / "chapters",
    }
    for d in subdirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return subdirs
