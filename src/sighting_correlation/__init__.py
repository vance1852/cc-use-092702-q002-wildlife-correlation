"""多源野生动物目击记录的关联、归并与统一事件治理。"""

from .contracts import SightingRecord, SightingValidationError
from .linking import ALGORITHM_VERSION, evaluate_links
from .service import SightingLinkService

__all__ = [
    "SightingRecord",
    "SightingValidationError",
    "ALGORITHM_VERSION",
    "SightingLinkService",
    "evaluate_links",
]

__version__ = "0.1.0"
