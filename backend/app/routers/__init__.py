"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import coord_window, resource

# 固定前缀的窗口路由必须排在通用 /api/{module} 之前，否则会被动态模块名吃掉。
ROUTERS = [coord_window, resource]
