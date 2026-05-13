"""``PlatformFactory`` —— 装配单个 OASIS Platform 实例.

LAYOUT §2.D / impl/pools_platform_factory.md:

    每池独立 Channel + 独立 ``pool_*.db`` (跑 OASIS schema) + 独立 :class:`RecSys`
    实例 + 一个 forked :class:`oasis.social_platform.Platform` (新签名携带 ``recsys``
    参数). 工厂只负责 **wiring**——pool DB 生命周期由 :class:`PoolDB` 负责.

调用方:

* :class:`MultiPoolPlatformManager` (L4) 启动期 + 仿真中途加池.

下游被调:

* :func:`oasis.social_platform.database.create_db` (PoolDB.init 内部已调,
  这里幂等再调一次以兼容直接使用 Factory 的场景).
* :class:`oasis.social_platform.channel.Channel` (每池一份).
* :class:`oasis.social_platform.recsys.RecSys` (每池一份).
* :class:`oasis.social_platform.platform.Platform` (forked, 新签名).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from oasis.clock.clock import Clock
    from oasis.social_platform.platform import Platform

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PlatformFactoryConfig:
    """Factory 级配置 (来自 ``simulation_config.json.pool_factory``)."""

    pool_dir_template: str = "{simulation_dir}/pools"
    pool_db_template: str = "pool__{place_id}__{feed_type}.db"
    recsys_defaults: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sandbox_clock: bool = True
    # Forwarded as-is to ``Platform.__init__``; allows runtime to override
    # OASIS knobs (refresh_rec_post_count / max_rec_post_len / ...) without
    # touching the factory code.
    platform_defaults: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class PlatformFactory:
    """Stateless wiring of ``Channel + RecSys + Platform`` for one pool.

    The factory does **not** track produced platforms; that bookkeeping is
    :class:`MultiPoolPlatformManager`'s job. Each :meth:`build_platform`
    invocation is independent.
    """

    def __init__(
        self,
        cfg: Optional[PlatformFactoryConfig] = None,
        clock: Optional["Clock"] = None,
    ) -> None:
        self.cfg = cfg or PlatformFactoryConfig()
        self.clock = clock

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def build_platform(
        self,
        pool_id: str,
        db_path: str,
        recsys_type: str,
        **kwargs: Any,
    ) -> "Platform":
        """Construct a fully-wired OASIS :class:`Platform` for ``pool_id``.

        Steps (LAYOUT §2.D / impl/pools_platform_factory.md §4.2):

        1. Ensure the pool DB file exists with the OASIS schema applied
           (``database.create_db``). PoolDB.init does this, but the factory
           is also a valid entry point so we run it idempotently.
        2. Construct an isolated :class:`Channel` (per-pool action queue).
        3. Construct a per-pool :class:`RecSys` instance keyed by
           ``recsys_type`` (a.k.a. feed_type at the LAYOUT level).
        4. Construct the forked :class:`Platform` with explicit
           ``channel=`` + ``recsys=`` injection.

        Parameters
        ----------
        pool_id:
            Logical id of the pool (used for logging only — DB path is
            taken from ``db_path``).
        db_path:
            Absolute path to ``pool_*.db``. Created if missing.
        recsys_type:
            RecSys algorithm selector (`twitter` / `reddit` / `twhin` /
            `random` / `lunar_net` / ...). Forwarded to ``RecSys.__init__``
            as ``rec_type=``.
        **kwargs:
            Extra knobs split between :class:`RecSys` and :class:`Platform`
            via ``recsys_kwargs=`` / ``platform_kwargs=`` sub-dicts; any
            other keys are passed straight through to the :class:`Platform`
            constructor (back-compat).
        """

        # OASIS imports are lazy: this module is part of the L3 boot path
        # and we don't want to pay the OASIS import cost on every L0/L1
        # touch (e.g., test collection in unrelated subsystems).
        from oasis.social_platform.channel import Channel
        from oasis.social_platform.database import create_db
        from oasis.social_platform.platform import Platform
        from oasis.social_platform.recsys import RecSys

        recsys_kwargs: Dict[str, Any] = dict(
            self.cfg.recsys_defaults.get(recsys_type, {})
        )
        recsys_kwargs.update(kwargs.pop("recsys_kwargs", {}) or {})

        platform_kwargs: Dict[str, Any] = dict(self.cfg.platform_defaults)
        platform_kwargs.update(kwargs.pop("platform_kwargs", {}) or {})
        # Any leftover kwargs are treated as Platform overrides for ergonomic
        # call sites: ``factory.build_platform(..., show_score=True)``.
        platform_kwargs.update(kwargs)

        # 1) DB schema (idempotent). PoolDB.init usually has already run;
        #    create_db tolerates re-open and just (re)connects.
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        try:
            create_db(db_path)
        except Exception as e:  # pragma: no cover - best-effort
            # Re-opening an existing DB can raise on duplicate DDL; that's
            # fine for our purposes — Platform.__init__ will reconnect.
            log.debug(
                "PlatformFactory(%s): create_db(%s) raised (likely re-open): %s",
                pool_id, db_path, e,
            )

        # 2) Per-pool Channel.
        channel = Channel()

        # 3) Per-pool RecSys.
        recsys = RecSys(rec_type=recsys_type, **recsys_kwargs)

        # 4) Platform (forked: accepts ``recsys=`` injection).
        platform = Platform(
            db_path=db_path,
            channel=channel,
            sandbox_clock=self.clock,
            recsys=recsys,
            recsys_type=recsys_type,
            **platform_kwargs,
        )

        log.info(
            "PlatformFactory: built pool_id=%s feed=%s db=%s",
            pool_id, recsys_type, db_path,
        )
        return platform

    # ------------------------------------------------------------------
    # path helpers (shared with PoolDB / Manager)
    # ------------------------------------------------------------------

    def resolve_db_path(
        self,
        simulation_dir: str,
        place_id: str,
        feed_type: str,
    ) -> str:
        """Render the ``pool_*.db`` path from templates in :attr:`cfg`.

        Provided as a convenience for callers that want to share the
        Factory's path conventions; not invoked internally by
        :meth:`build_platform` (caller passes ``db_path`` directly).
        """
        pool_dir = self.cfg.pool_dir_template.format(simulation_dir=simulation_dir)
        fname = self.cfg.pool_db_template.format(
            place_id=place_id, feed_type=feed_type
        )
        return os.path.join(pool_dir, fname)


__all__ = ["PlatformFactory", "PlatformFactoryConfig"]
