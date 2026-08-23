"""OmicsPrism Copilot 的受控 agent 运行时契约。"""

from .schemas import (
    AgentDecision,
    GroundedAnswer,
    ModelContext,
    RouteDecision,
    RunState,
    ToolResult,
    VerifierVerdict,
    PlanRecord,
)
from .approvals import InMemoryApprovalGate, JsonApprovalGate, PostgresApprovalGate
from .audit import PostgresAgentEventStore
from .context import MinimalContextBuilder
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
    resolve_analysis_request,
)
from .validation import DatasetRef, Issue, ValidationReport, validate_analysis_request
from .eval import EvalAssembly, EvalAssemblyFactory, EvalRunner, compare_reports, load_golden_cases
from .model import ScriptedModelAdapter, VllmModelAdapter
from .policy import ProfilePolicyGuard
from .router import RuleRouter
from .runtime import ProductionRunCoordinator
from .store import InMemoryStateStore, PostgresStateStore
from .plans import InMemoryPlanStore, JsonPlanStore, PostgresPlanStore
from .product_store import InMemoryAgentProductStore, PostgresAgentProductStore
from .tools import (
    AgentInputFile,
    AgentToolRuntime,
    ExistingJobInputSource,
    PolicyToolExecutor,
    StagedBundleInputSource,
    ToolConfigurationError,
    ToolRegistry,
)

__all__ = [
    "AgentDecision",
    "GroundedAnswer",
    "ModelContext",
    "RouteDecision",
    "RunState",
    "ToolResult",
    "VerifierVerdict",
    "InMemoryApprovalGate",
    "JsonApprovalGate",
    "PostgresApprovalGate",
    "PostgresAgentEventStore",
    "MinimalContextBuilder",
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
    "resolve_analysis_request",
    "DatasetRef",
    "Issue",
    "ValidationReport",
    "validate_analysis_request",
    "compute_input_fingerprint",
    "ScriptedModelAdapter",
    "VllmModelAdapter",
    "EvalAssembly",
    "EvalAssemblyFactory",
    "EvalRunner",
    "compare_reports",
    "load_golden_cases",
    "ProfilePolicyGuard",
    "RuleRouter",
    "ProductionRunCoordinator",
    "InMemoryStateStore",
    "PostgresStateStore",
    "InMemoryPlanStore",
    "PlanRecord",
    "JsonPlanStore",
    "PostgresPlanStore",
    "InMemoryAgentProductStore",
    "PostgresAgentProductStore",
    "AgentInputFile",
    "AgentToolRuntime",
    "ExistingJobInputSource",
    "StagedBundleInputSource",
    "PolicyToolExecutor",
    "ToolConfigurationError",
    "ToolRegistry",
]
