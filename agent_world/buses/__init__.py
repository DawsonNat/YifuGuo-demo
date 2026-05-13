"""Agent World direct-communication buses (L3).

Three async buses fan out into ``world.db.direct_message`` (plus group_*) for
PerceptionBuilder consumption. F2F / RDC / GRP each enforce their own
``phi_*`` predicate from :class:`ConnectivityResolver`. All writes go through
:class:`WorldDB`'s single asyncio.Lock (LAYOUT B8); no bus owns its own lock.

- :class:`FaceToFaceBus`    — F2F same-place broadcast (immediate)
- :class:`RemoteMessageBus` — RDC point-to-point (lockstep, with delay)
- :class:`GroupMessageBus`  — GRP many-to-many (B6 persistent queue, P6)
"""

from agent_world.buses.face_to_face import FaceToFaceBus
from agent_world.buses.group_message import GroupMessageBus
from agent_world.buses.remote_message import RemoteMessageBus, SendResult

__all__ = ["FaceToFaceBus", "GroupMessageBus", "RemoteMessageBus", "SendResult"]
