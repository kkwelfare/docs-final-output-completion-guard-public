"""Quality-portable producer and host seams for the docs completion guard.

The package is deliberately local-first: the bundled deterministic checker and
rule identifiers decide acceptance; host renderers and advisory providers are
optional adapters and cannot upgrade a blocked local result.
"""

from .host_adapter import HostAdapter
from .producer_core import ProducerRequest, produce

__all__ = ["HostAdapter", "ProducerRequest", "produce"]
