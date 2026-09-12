"""Onboarding steps, independent verifiers, and focused execution."""

from dataclasses import asdict, dataclass, replace
from enum import Enum
from time import monotonic

from . import (
    codex_hooks,
    codex_skills,
    discovery,
    embedding,
    history_index,
    mcp_registration,
    retrieval_health,
    service,
    storage,
)


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
    optional: bool = False

    def install(self) -> None:
        """Reserved for a future implementation; performs no changes."""

    def skip(self) -> None:
        """Record an explicit skip for an optional installation step."""

    def verify(self) -> CheckResult:
        return CheckResult(
            self.step_id, self.label, CheckStatus.FAILED, "Not implemented",
            diagnostic="This release defines the CLI contract only.",
            remediation="Review the development roadmap; rerunning cannot repair this yet.",
        )


class DataDirectoryStep(Step):
    def install(self):
        storage.install()

    def verify(self):
        try:
            summary = storage.verify()
            return CheckResult(self.step_id, self.label, CheckStatus.PASSED, summary)
        except Exception as exc:
            return CheckResult(
                self.step_id, self.label, CheckStatus.FAILED, "Data installation unavailable",
                diagnostic=str(exc),
                remediation="Run blc install codex --step data_directory; inspect ownership and permissions if it fails.",
            )


class BackgroundServiceStep(Step):
    def install(self):
        service.install()

    def verify(self):
        try:
            return CheckResult(self.step_id, self.label, CheckStatus.PASSED, service.verify())
        except Exception as exc:
            return CheckResult(self.step_id, self.label, CheckStatus.FAILED,
                               "Background service unavailable", diagnostic=str(exc),
                               remediation="Run blc install codex --step background_service. " + service.diagnostics())


class CodexMcpStep(Step):
    def install(self):
        mcp_registration.install()

    def verify(self):
        try:
            return CheckResult(self.step_id, self.label, CheckStatus.PASSED, mcp_registration.verify())
        except Exception as exc:
            return CheckResult(self.step_id, self.label, CheckStatus.FAILED, "Codex MCP unavailable",
                               diagnostic=str(exc), remediation="Run blc install codex --step codex_mcp.")

class CodexSkillsStep(Step):
    def install(self):
        codex_skills.install()

    def verify(self):
        try:
            return CheckResult(self.step_id, self.label, CheckStatus.PASSED, codex_skills.verify())
        except Exception as exc:
            return CheckResult(self.step_id, self.label, CheckStatus.FAILED, "Codex skill unavailable",
                               diagnostic=str(exc), remediation="Run blc install codex --step codex_skills.")


class EmbeddingModelStep(Step):
    def install(self):
        embedding.install()

    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, embedding.verify()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                "Embedding model unavailable",
                diagnostic=str(exc),
                remediation=(
                    "Run blc install codex --step embedding_model; "
                    "completed downloads are reused."
                ),
            )


class SessionDiscoveryStep(Step):
    def install(self):
        discovery.install()

    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, discovery.verify()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                "Codex session discovery unavailable",
                diagnostic=str(exc),
                remediation="Run blc install codex --step session_discovery.",
            )


class HistoryIndexStep(Step):
    def install(self):
        history_index.install()

    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, history_index.verify()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                "Recent Codex history is not indexed",
                diagnostic=str(exc),
                remediation="Run blc install codex --step history_index.",
            )


class ServiceHealthStep(Step):
    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, service.health()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                'Service health unavailable',
                diagnostic=str(exc),
                remediation='Run blc doctor --step background_service.',
            )


class McpHealthStep(Step):
    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, mcp_registration.health()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                'MCP connection unavailable',
                diagnostic=str(exc),
                remediation='Run blc doctor --step codex_mcp.',
            )


class HistoryRetrievalStep(Step):
    def verify(self):
        try:
            return CheckResult(
                self.step_id, self.label, CheckStatus.PASSED, retrieval_health.verify()
            )
        except Exception as exc:
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.FAILED,
                'Historical memory retrieval unavailable',
                diagnostic=str(exc),
                remediation='Run blc doctor --step history_retrieval.',
            )


class CodexHooksStep(Step):
    def install(self):
        codex_hooks.install()

    def skip(self):
        codex_hooks.skip()

    def verify(self):
        if codex_hooks.skipped():
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.PASSED,
                "Hooks not installed.",
                skip_reason="optional",
            )
        try:
            codex_hooks.verify_registration()
            return CheckResult(
                self.step_id,
                self.label,
                CheckStatus.PASSED,
                "Approve hooks on next Codex launch.",
            )
        except Exception as exc:
            return CheckResult(self.step_id, self.label, CheckStatus.FAILED, 'Codex hooks unavailable',
                               diagnostic=str(exc), remediation='Review Context hooks in Codex /hooks, complete a new conversation, then run blc doctor --step codex_hooks.')


@dataclass(frozen=True)
class UninstallStep(Step):
    purge: bool = False

    def install(self):
        codex_hooks.uninstall()
        codex_skills.uninstall()
        mcp_registration.uninstall()
        service.uninstall()
        storage.uninstall(purge=self.purge)

    def verify(self):
        storage.verify_uninstalled()
        return CheckResult(self.step_id, self.label, CheckStatus.PASSED,
                           "Data installation disconnected." if not self.purge else "Owned data purged; unrelated files preserved.")


STEPS = (
    DataDirectoryStep("data_directory", "Data directory initialized"),
    BackgroundServiceStep("background_service", "Background service installed", prerequisites=("data_directory",)),
    CodexMcpStep("codex_mcp", "Codex MCP registered", prerequisites=("background_service",)),
    CodexSkillsStep("codex_skills", "Codex skills installed", prerequisites=("data_directory",)),
    
    EmbeddingModelStep(
        "embedding_model", "Embedding model available", prerequisites=("data_directory",)
    ),
    SessionDiscoveryStep(
        "session_discovery",
        "Existing Codex sessions discovered",
        history=True,
        prerequisites=("data_directory",),
    ),
    HistoryIndexStep(
        "history_index",
        "Historical sessions indexed",
        history=True,
        prerequisites=("background_service", "session_discovery", "embedding_model"),
    ),
    ServiceHealthStep(
        "service_health",
        "Service healthy",
        prerequisites=("background_service", "embedding_model"),
    ),
    McpHealthStep(
        "mcp_health",
        "MCP connection healthy",
        prerequisites=("codex_mcp", "service_health"),
    ),
    HistoryRetrievalStep(
        "history_retrieval",
        "Historical memory retrieval verified",
        history=True,
        prerequisites=("history_index", "mcp_health"),
    ),
    CodexHooksStep(
        "codex_hooks",
        "Codex Hooks",
        prerequisites=("codex_mcp", "codex_skills"),
        optional=True,
    ),
)

UNINSTALL_STEPS = (
    UninstallStep("uninstall_codex", "Context installation deactivated"),
)


def select_steps(steps, selected_step, install):
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


def run_checks(
    steps=None,
    *,
    install=False,
    no_history=False,
    selected_step=None,
    on_step_start=None,
    on_result=None,
    should_install=None,
):
    """Install prerequisites first, then independently verify every executed step.

    Verification commands inspect only the selected step. Full reports preserve
    transcript order even when installation requires a different execution order.
    """
    steps = STEPS if steps is None else tuple(steps)
    by_id = {step.step_id: step for step in steps}
    ordered = select_steps(steps, selected_step, install)
    if selected_step and no_history and any(s.history for s in ordered):
        raise ValueError("--step requires history; cannot combine with --no-history")
    results = {}
    for step in ordered:
        if on_step_start:
            on_step_start(step)
        started = monotonic()
        if no_history and step.history:
            result = CheckResult(
                step.step_id, step.label, CheckStatus.SKIPPED,
                "Skipped (--no-history)", skip_reason="no_history",
            )
        else:
            blocked = [
                prerequisite
                for prerequisite in step.prerequisites
                if install
                and not _result_succeeded(
                    results[prerequisite], by_id[prerequisite], no_history
                )
            ]
            try:
                if blocked:
                    result = CheckResult(
                        step.step_id, step.label, CheckStatus.FAILED,
                        "Prerequisites unavailable", diagnostic=", ".join(blocked),
                        remediation="Resolve the failed prerequisite checks first.",
                    )
                else:
                    if install:
                        if should_install and not should_install(step):
                            step.skip()
                        else:
                            step.install()
                    result = step.verify()
                    if result.step_id != step.step_id:
                        raise ValueError("Verifier returned the wrong step ID")
            except Exception as exc:
                result = CheckResult(
                    step.step_id, step.label, CheckStatus.FAILED,
                    "Check failed", diagnostic=str(exc),
                    remediation="Run blc doctor for diagnostics.",
                )
        results[step.step_id] = replace(
            result, duration=monotonic() - started,
            dependency=bool(selected_step and step.step_id != selected_step),
        )
        if on_result:
            on_result(results[step.step_id])
    display = ordered if selected_step else steps
    return [results[s.step_id] for s in display]


def _result_succeeded(result, step, no_history):
    return (
        result.status == CheckStatus.PASSED
        or (step.optional and result.status in (CheckStatus.WARNING, CheckStatus.SKIPPED))
        or (
            no_history
            and step.history
            and result.status == CheckStatus.SKIPPED
            and result.skip_reason == "no_history"
        )
    )


def checks_succeeded(results, *, no_history=False, steps=None):
    steps = STEPS if steps is None else steps
    by_id = {step.step_id: step for step in steps}
    return bool(results) and all(
        _result_succeeded(
            result,
            by_id.get(result.step_id, Step(result.step_id, result.label)),
            no_history,
        )
        for result in results
    )


def is_ready(results, *, no_history=False, selected_step=None):
    return (
        selected_step is None
        and [r.step_id for r in results] == [s.step_id for s in STEPS]
        and checks_succeeded(results, no_history=no_history)
    )
