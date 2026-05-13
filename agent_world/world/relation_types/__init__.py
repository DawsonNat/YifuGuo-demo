"""RelationType 子类目录。

继承 ``agent_world.world._registrars.RelationBase`` 即自动注册。启动期
runner 调 ``conscribe.discover("agent_world.world.relation_types")`` 触发
import。

MVP 内置子类（如 ``LoverRelation`` / ``FriendRelation`` 等）将在 L1
``world_relation_graph`` 阶段补齐；本目录目前为空占位。
"""
