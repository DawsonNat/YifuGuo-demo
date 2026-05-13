"""Agent World runner subprocess (LAYOUT §2.I).

The runner is the simulation's main entry point — it is started by
``app/services/simulation_manager.py`` via ``subprocess.Popen`` and
wires :mod:`agent_world.world`, :mod:`agent_world.buses`,
:mod:`agent_world.pools`, :mod:`agent_world.script`,
:mod:`agent_world.memory`, :mod:`agent_world.persistence`, and
:mod:`agent_world.ipc` into a runnable simulation.

Public surface:

* :func:`run_agent_world_simulation.main` -- async main; the
  ``python -m agent_world.runner.run_agent_world_simulation`` entry point.
* :class:`action_logger.ActionLogger` -- ``actions.jsonl`` writer with
  the v0.3 fields (``place_id`` / ``channel_type`` / ``arrive_at`` /
  ``attempted_at`` / ``delivered``).
"""

from agent_world.runner.action_logger import ActionLogger

__all__ = ["ActionLogger"]
