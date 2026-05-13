"""agent_world.memory package.

L1 primitives:
    - translator: pure function turning a structured action / event dict
      into a single-line natural-language sentence.
    - retrieval:  MultiGraphRetriever, parallel Zep quick_search wrapper.

Higher-level modules (segment / updater / manager / compressor) live in
later phases (L2 / L3) and import from this package.
"""

from agent_world.memory.translator import (
    translate,
    translate_event,
    register_translator,
)
from agent_world.memory.retrieval import MultiGraphRetriever, RetrievedMemory
from agent_world.memory.segment import RawEntry, SegmentStore
from agent_world.memory.updater import MultiGraphUpdater, BufferedEpisode
from agent_world.memory.manager import MultiGraphManager

__all__ = [
    "translate",
    "translate_event",
    "register_translator",
    "MultiGraphRetriever",
    "RetrievedMemory",
    "RawEntry",
    "SegmentStore",
    "MultiGraphUpdater",
    "BufferedEpisode",
    "MultiGraphManager",
]
