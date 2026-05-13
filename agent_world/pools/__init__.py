"""Agent World ``pools/`` 子系统：多池 OASIS Platform 装配 + 路由。

L3 暴露 :class:`PlatformFactory`；上层 ``MultiPoolPlatformManager`` (L4) 在此
之上做注册 / 路由 / 启动恢复编排。
"""

from agent_world.pools.platform_factory import (
    PlatformFactory,
    PlatformFactoryConfig,
)

__all__ = ["PlatformFactory", "PlatformFactoryConfig"]
