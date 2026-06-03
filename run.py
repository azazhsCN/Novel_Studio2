"""Novel_Studio2 启动脚本"""
import uvicorn
from app.core.config import load_config

if __name__ == "__main__":
    cfg = load_config()
    server = cfg.get("server", {})
    uvicorn.run(
        "app.main:app",
        host=server.get("host", "127.0.0.1"),
        port=server.get("port", 8000),
        reload=True,
    )
