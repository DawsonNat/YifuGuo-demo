"""Agent World ``script/`` 子系统：剧本 (event = trigger + effect) 执行栈。

L0 仅暴露 conscribe Registrar 中枢（``_registrars``）；具体的
``effects/`` / ``triggers/`` / ``loader`` / ``engine`` 在 L1~L3 增量补齐。
"""
