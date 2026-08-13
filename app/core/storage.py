"""文件存储工具：文件名消毒、备份轮转、回收站移动"""
import re
import shutil
from datetime import datetime
from pathlib import Path

# Windows/跨平台文件名非法字符与控制字符
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f]')
# 非法字符替换为全角，保持文件名可读
_FULLWIDTH_MAP = str.maketrans({
    '\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？',
    '"': '＂', '<': '＜', '>': '＞', '|': '｜',
})
# Windows 保留设备名（无论扩展名如何都非法）
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"}
for _i in range(1, 10):
    _WINDOWS_RESERVED.add(f"com{_i}")
    _WINDOWS_RESERVED.add(f"lpt{_i}")


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清理文件名中的非法字符（全角替换）、控制字符、保留名与尾随点/空格"""
    name = _INVALID_FILENAME_CHARS.sub('_', name).translate(_FULLWIDTH_MAP)
    name = name.rstrip('. ').strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip('. ')
    stem = name.split('.')[0].lower()
    if stem in _WINDOWS_RESERVED:
        name = f"_{name}"
    if not name:
        name = "未命名"
    return name


def backup_file(path: Path, keep: int = 2) -> None:
    """覆盖写之前把现有文件轮转为 .bak（保留最近 keep 份）"""
    if not path.exists():
        return
    # 旧备份逐级轮转：.bak{i+1} ← .bak{i}
    for i in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.bak{i}")
        dst = path.with_name(f"{path.name}.bak{i + 1}")
        if src.exists():
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
    # 当前文件复制为最新备份
    shutil.copy2(str(path), str(path.with_name(f"{path.name}.bak")))


def move_to_trash(path: Path, trash_dir: Path) -> Path:
    """把文件/目录移入回收站（带时间戳），代替不可逆删除"""
    trash_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = trash_dir / f"{path.name}_{ts}"
    n = 1
    while target.exists():
        target = trash_dir / f"{path.name}_{ts}_{n}"
        n += 1
    shutil.move(str(path), str(target))
    return target


def quarantine_corrupt_file(path: Path) -> None:
    """把损坏文件改名 .corrupt-时间戳 留证，避免被误覆盖或反复报错"""
    if not path.exists():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        path.rename(target)
    except OSError:
        pass
