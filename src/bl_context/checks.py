"""The onboarding contract. All production checks intentionally fail for now."""

from dataclasses import asdict, dataclass, replace
from enum import Enum
from time import monotonic


class CheckStatus(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    step_id: str
    label: str
    status: CheckStatus
    summary: str
    diagnostic: str = ""
    remediation: str = ""
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {**asdict(self), "status": self.status.value}


@dataclass(frozen=True)
class Step:
    step_id: str
    label: str
    history: bool = False

    def install(self) -> None:
        """Reserved for a future implementation; performs no changes."""

    def verify(self) -> CheckResult:
        return CheckResult(
            self.step_id, self.label, CheckStatus.FAILED, "Not implemented",
            diagnostic="This release defines the CLI contract only.",
            remediation="Review the development roadmap; rerunning cannot repair this yet.",
        )


STEPS = (
    Step("data_directory", "Data directory initialized"),
    Step("background_service", "Background service installed"),
    Step("codex_mcp", "Codex MCP registered"),
    Step("codex_skills", "Codex skills installed"),
    Step("codex_hooks", "Codex hooks installed"),
    Step("embedding_model", "Embedding model available"),
    Step("session_discovery", "Existing Codex sessions discovered", history=True),
    Step("history_index", "Historical sessions indexed", history=True),
    Step("service_health", "Service healthy"),
    Step("mcp_health", "MCP connection healthy"),
    Step("history_retrieval", "Historical memory retrieval verified", history=True),
)

UNINSTALL_STEPS = (
    Step("uninstall_codex", "Codex integration removed"),
)


def run_checks(steps=STEPS, *, install=False, no_history=False):
    """Always verify after installation; an install return value is not evidence."""
    results = []
    for step in steps:
        started = monotonic()
        if no_history and step.history:
            result = CheckResult(
                step.step_id, step.label, CheckStatus.SKIPPED,
                "Skipped (--no-history)",
            )
        else:
            try:
                if install:
                    step.install()
                result = step.verify()
            except Exception as exc:
                result = CheckResult(
                    step.step_id, step.label, CheckStatus.FAILED,
                    "Check failed", diagnostic=str(exc),
                    remediation="Run blctx doctor for diagnostics.",
                )
        results.append(replace(result, duration=monotonic() - started))
    return results


def is_ready(results):
    return bool(results) and all(
        r.status == CheckStatus.PASSED
        or (r.status == CheckStatus.SKIPPED and r.summary == "Skipped (--no-history)")
        for r in results
    )
