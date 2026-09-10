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
    skip_reason: str | None = None
    dependency: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), "status": self.status.value}


@dataclass(frozen=True)
class Step:
    step_id: str
    label: str
    history: bool = False
    prerequisites: tuple[str, ...] = ()

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
    Step("background_service", "Background service installed", prerequisites=("data_directory",)),
    Step("codex_mcp", "Codex MCP registered", prerequisites=("background_service",)),
    Step("codex_skills", "Codex skills installed", prerequisites=("data_directory",)),
    Step("codex_hooks", "Codex hooks installed", prerequisites=("data_directory",)),
    Step("embedding_model", "Embedding model available", prerequisites=("data_directory",)),
    Step("session_discovery", "Existing Codex sessions discovered", history=True, prerequisites=("data_directory",)),
    Step("history_index", "Historical sessions indexed", history=True,
         prerequisites=("session_discovery", "embedding_model")),
    Step("service_health", "Service healthy", prerequisites=("background_service", "embedding_model")),
    Step("mcp_health", "MCP connection healthy", prerequisites=("codex_mcp", "service_health")),
    Step("history_retrieval", "Historical memory retrieval verified", history=True,
         prerequisites=("history_index", "mcp_health")),
)

UNINSTALL_STEPS = (
    Step("uninstall_codex", "Codex integration removed"),
)


def _selection(steps, selected_step, install):
    by_id = {s.step_id: s for s in steps}
    if len(by_id) != len(steps):
        raise ValueError("Duplicate step IDs")
    if selected_step is not None and selected_step not in by_id:
        raise ValueError(f"Unknown step: {selected_step}")
    ordered, visiting, visited = [], set(), set()

    def visit(step_id):
        if step_id in visiting:
            raise ValueError(f"Cyclic prerequisite: {step_id}")
        if step_id not in by_id:
            raise ValueError(f"Unknown prerequisite: {step_id}")
        if step_id in visited:
            return
        visiting.add(step_id)
        if install:
            for prerequisite in by_id[step_id].prerequisites:
                visit(prerequisite)
        visiting.remove(step_id)
        visited.add(step_id)
        ordered.append(by_id[step_id])

    for step_id in ([selected_step] if selected_step else by_id):
        visit(step_id)
    return ordered


def run_checks(steps=None, *, install=False, no_history=False, selected_step=None):
    """Install prerequisites first, then independently verify every executed step.

    Verification commands inspect only the selected step. Full reports preserve
    transcript order even when installation requires a different execution order.
    """
    steps = STEPS if steps is None else tuple(steps)
    ordered = _selection(steps, selected_step, install)
    if selected_step and no_history and any(s.history for s in ordered):
        raise ValueError("--step requires history; cannot combine with --no-history")
    results = {}
    for step in ordered:
        started = monotonic()
        if no_history and step.history:
            result = CheckResult(
                step.step_id, step.label, CheckStatus.SKIPPED,
                "Skipped (--no-history)", skip_reason="no_history",
            )
        else:
            blocked = [p for p in step.prerequisites
                       if install and results[p].status != CheckStatus.PASSED]
            try:
                if blocked:
                    result = CheckResult(
                        step.step_id, step.label, CheckStatus.FAILED,
                        "Prerequisites unavailable", diagnostic=", ".join(blocked),
                        remediation="Resolve the failed prerequisite checks first.",
                    )
                else:
                    if install:
                        step.install()
                    result = step.verify()
                    if result.step_id != step.step_id:
                        raise ValueError("Verifier returned the wrong step ID")
            except Exception as exc:
                result = CheckResult(
                    step.step_id, step.label, CheckStatus.FAILED,
                    "Check failed", diagnostic=str(exc),
                    remediation="Run blctx doctor for diagnostics.",
                )
        results[step.step_id] = replace(
            result, duration=monotonic() - started,
            dependency=bool(selected_step and step.step_id != selected_step),
        )
    display = ordered if selected_step else steps
    return [results[s.step_id] for s in display]


def checks_succeeded(results, *, no_history=False, steps=None):
    steps = STEPS if steps is None else steps
    history_ids = {s.step_id for s in steps if s.history}
    return bool(results) and all(
        r.status == CheckStatus.PASSED
        or (no_history and r.step_id in history_ids
            and r.status == CheckStatus.SKIPPED and r.skip_reason == "no_history")
        for r in results
    )


def is_ready(results, *, no_history=False, selected_step=None):
    return (
        selected_step is None
        and [r.step_id for r in results] == [s.step_id for s in STEPS]
        and checks_succeeded(results, no_history=no_history)
    )
