"""OmicsPrism Copilot 的受控 agent 运行时契约。"""

from .schemas import GroundedAnswer, RunState, ToolResult, VerifierVerdict
from .dataset_profile import DatasetProfile, MatrixProfile, MetadataProfile, build_dataset_profiles
from .fingerprint import compute_input_fingerprint
from .param_resolver import (
    AnalysisParams,
    AnalysisProposal,
    ContrastSpec,
    DEGParams,
    DEMParams,
    GMAParams,
    MissingParam,
    ResolvedRequest,
    ScopeSpec,
    ScopeMode,
    resolve_analysis_request,
)
from .validation import (
    DatasetRef,
    Issue,
    ValidationReport,
    derive_scoped_dataset_refs,
    validate_analysis_request,
)
from .model import VllmGraphModel
from .product_store import InMemoryAgentProductStore, PostgresAgentProductStore
from .tools import (
    AgentInputFile,
    AgentToolRuntime,
    ToolConfigurationError,
)

__all__ = [
    "GroundedAnswer",
    "RunState",
    "ToolResult",
    "VerifierVerdict",
    "DatasetProfile",
    "MatrixProfile",
    "MetadataProfile",
    "build_dataset_profiles",
    "AnalysisParams",
    "AnalysisProposal",
    "ContrastSpec",
    "DEGParams",
    "DEMParams",
    "GMAParams",
    "MissingParam",
    "ResolvedRequest",
    "ScopeSpec",
    "ScopeMode",
    "resolve_analysis_request",
    "DatasetRef",
    "Issue",
    "ValidationReport",
    "derive_scoped_dataset_refs",
    "validate_analysis_request",
    "compute_input_fingerprint",
    "VllmGraphModel",
    "InMemoryAgentProductStore",
    "PostgresAgentProductStore",
    "AgentInputFile",
    "AgentToolRuntime",
    "ToolConfigurationError",
]
