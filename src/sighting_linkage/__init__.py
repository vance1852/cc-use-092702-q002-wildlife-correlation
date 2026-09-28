"""多源野生动物目击记录关联归并服务。"""

from .linkage import ALGORITHM_VERSION, StoredRecord, build_pairwise, compare_records
from .models import ImageSummary, SightRecordInput
from .service import SightingLinkageService

__all__ = [
    "ALGORITHM_VERSION",
    "ImageSummary",
    "SightRecordInput",
    "SightingLinkageService",
    "StoredRecord",
    "build_pairwise",
    "compare_records",
]

__version__ = "0.1.0"
