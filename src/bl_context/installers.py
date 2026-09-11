"""Provider-specific installation adapters."""

from abc import ABC, abstractmethod
from dataclasses import replace

from . import checks, codex_hooks, embedding, storage

CODEX_PROVIDER = "openai/codex"


class InstallerAdapter(ABC):
    """Translate provider-independent installer operations to one provider."""

    provider: str

    @abstractmethod
    def install(self, *, no_history=False, selected_step=None, **observers):
        """Install the provider integration and return its check results."""

    @abstractmethod
    def verify(self, *, no_history=False, selected_step=None, **observers):
        """Verify the provider integration without changing it."""

    @abstractmethod
    def uninstall(self, *, purge=False, **observers):
        """Remove the provider integration and return its check results."""


class CodexInstallerAdapter(InstallerAdapter):
    """Install and verify the OpenAI Codex integration."""

    provider = CODEX_PROVIDER

    def install(self, *, no_history=False, selected_step=None, **observers):
        return self.codex_install(
            no_history=no_history, selected_step=selected_step, **observers
        )

    def codex_install(self, *, no_history=False, selected_step=None, **observers):
        """Run the Codex installation graph.

        This method is intentionally the only mutation path used by Codex install.
        Status and doctor call ``verify``; uninstall has its own explicit path.
        """
        choose_hooks = observers.pop("choose_hooks", None)

        def should_install(step):
            if step.step_id != "codex_hooks":
                return True
            if codex_hooks.registered():
                return True
            return choose_hooks() if choose_hooks else False

        return checks.run_checks(
            checks.STEPS,
            install=True,
            no_history=no_history,
            selected_step=selected_step,
            should_install=should_install,
            **observers,
        )

    def verify(self, *, no_history=False, selected_step=None, **observers):
        return checks.run_checks(
            checks.STEPS,
            no_history=no_history,
            selected_step=selected_step,
            **observers,
        )

    def uninstall(self, *, purge=False, **observers):
        steps = checks.UNINSTALL_STEPS
        if purge:
            steps = tuple(replace(step, purge=True) for step in steps)
        with embedding.cache_lock(storage.locations()):
            return checks.run_checks(steps, install=True, **observers)

    def display_steps(self, command, selected_step=None):
        if command == "uninstall":
            return checks.UNINSTALL_STEPS
        return checks.select_steps(
            checks.STEPS, selected_step, install=command == "install"
        )


def installer_for(provider):
    """Return the adapter for a canonical provider identifier."""
    if provider == CODEX_PROVIDER:
        return CodexInstallerAdapter()
    raise ValueError(f"Unsupported provider: {provider}")
