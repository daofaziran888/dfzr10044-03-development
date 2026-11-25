"""业务路由汇总：交接清单等专用路由先注册，通用资源路由兜底。"""
from __future__ import annotations

from app.routers import handoff, resource

ROUTERS = [handoff, resource]
