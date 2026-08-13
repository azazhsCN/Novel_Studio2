"""Novel_Studio2 启动脚本"""
import os
import uvicorn
from app.core.config import load_config, DATA_DIR


def _write_pid_file():
    """写 pid 文件供 stop.bat 精确定位本进程"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = DATA_DIR / "server.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    return pid_file


if __name__ == "__main__":
    cfg = load_config()
    server = cfg.get("server", {})
    pid_file = _write_pid_file()
    try:
        uvicorn.run(
            "app.main:app",
            host=server.get("host", "127.0.0.1"),
            port=server.get("port", 8000),
            reload=False,
        )
    finally:
        if pid_file.exists():
            pid_file.unlink()
