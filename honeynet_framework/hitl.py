"""HITL (Human-in-the-Loop) reviewer for repair proposals.

When enabled, the reviewer pauses the repair loop and asks the operator to
approve, skip, or modify low-confidence repair proposals before they are
applied.

Safe defaults
-------------
- Only activates when ``enable_hitl=True`` in OrchestratorConfig.
- Non-TTY environments (CI, Docker, no stdin) fall back to auto-approve so
  the pipeline is never blocked silently.
- The operator can type ``skip`` to reject a proposal or ``auto`` to approve
  all remaining proposals without further prompts.

Usage
-----
    hitl = HITLReviewer(trigger_threshold=0.3)
    proposals = await hitl.review(proposals, failure_contexts)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

try:
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable
    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RICH_AVAILABLE = False

try:
    from prompt_toolkit import prompt as _pt_prompt
    _PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMPT_TOOLKIT_AVAILABLE = False

from .repair_types import FailureContext, RepairProposal

logger = logging.getLogger(__name__)

_console = _RichConsole(stderr=False) if _RICH_AVAILABLE else None


def _is_interactive() -> bool:
    """Return True if stdin is connected to a real TTY."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


class HITLReviewer:
    """Interactive reviewer that pauses for human approval on risky proposals.

    Parameters
    ----------
    trigger_threshold:
        Proposals with ``score < trigger_threshold`` (or no score) trigger
        a human review prompt.  Set to 0.0 to review all proposals; set to
        1.1 to effectively disable HITL even when the class is instantiated.
    auto_approve:
        If True, skip all interactive prompts and approve everything.
        Automatically set to True when not running in a TTY.
    """

    def __init__(
        self,
        trigger_threshold: float = 0.3,
        auto_approve: bool = False,
    ) -> None:
        self.trigger_threshold = trigger_threshold
        self._auto = auto_approve or not _is_interactive()
        if self._auto:
            logger.info(
                "HITLReviewer: non-interactive environment detected — auto-approving all proposals"
            )

    async def review(
        self,
        proposals: list[RepairProposal],
        contexts: list[FailureContext],
    ) -> list[RepairProposal]:
        """Review proposals and mark skipped ones.

        Returns the (potentially mutated) proposal list.
        """
        if self._auto:
            return proposals

        ctx_by_name = {c.system_name: c for c in contexts}
        loop = asyncio.get_running_loop()

        for prop in proposals:
            # Only review proposals below the threshold (or unscored)
            score = prop.score
            if score is not None and score >= self.trigger_threshold:
                continue

            ctx = ctx_by_name.get(prop.system_name)
            self._print_proposal(prop, ctx)

            answer = await loop.run_in_executor(None, self._prompt_user)

            if answer == "auto":
                self._auto = True
                logger.info("HITLReviewer: operator chose auto-approve for remaining proposals")
                break
            elif answer == "skip":
                prop.applied = False
                prop.skip_reason = "operator rejected via HITL"
                logger.info("HITLReviewer: operator skipped repair for %s", prop.system_name)
            else:
                # Approved (enter / 'y' / 'yes' / anything else)
                logger.info("HITLReviewer: operator approved repair for %s", prop.system_name)

        return proposals

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _print_proposal(prop: RepairProposal, ctx: Optional[FailureContext]) -> None:
        score_str = f"{prop.score:.2f}" if prop.score is not None else "N/A"
        failure_str = ctx.failure_type.value if ctx else "unknown"
        diagnosis_str = ctx.diagnosis if ctx else ""

        if _RICH_AVAILABLE and _console is not None:
            table = _RichTable(
                title=f"[bold yellow]HITL[/bold yellow] Low-confidence repair proposal (score={score_str})",
                show_header=True,
                header_style="bold cyan",
                min_width=60,
            )
            table.add_column("Field", style="bold", no_wrap=True)
            table.add_column("Value")
            table.add_row("System", prop.system_name)
            table.add_row("Failure", failure_str)
            if diagnosis_str:
                table.add_row("Diagnosis", diagnosis_str)
            table.add_row("Original image", prop.original_image)
            table.add_row("Proposed image", f"[green]{prop.proposed_image}[/green]")
            if prop.proposed_command:
                table.add_row("Command", str(prop.proposed_command))
            if prop.score_reason:
                table.add_row("Reason", prop.score_reason)
            _console.print()
            _console.print(table)
        else:
            print("\n" + "=" * 60, flush=True)
            print(f"[HITL] Low-confidence repair proposal (score={score_str})", flush=True)
            print(f"  System    : {prop.system_name}", flush=True)
            print(f"  Failure   : {failure_str}", flush=True)
            if diagnosis_str:
                print(f"  Diagnosis : {diagnosis_str}", flush=True)
            print(f"  Original  : {prop.original_image}", flush=True)
            print(f"  Proposed  : {prop.proposed_image}", flush=True)
            if prop.proposed_command:
                print(f"  Command   : {prop.proposed_command}", flush=True)
            if prop.score_reason:
                print(f"  Reason    : {prop.score_reason}", flush=True)
            print("=" * 60, flush=True)

    @staticmethod
    def _prompt_user() -> str:
        """Read one line from stdin.  Returns normalised answer."""
        prompt_text = "  [y/enter] approve  [skip] reject  [auto] approve all > "
        try:
            if _PROMPT_TOOLKIT_AVAILABLE:
                raw = _pt_prompt(prompt_text).strip().lower()
            else:
                raw = input(prompt_text).strip().lower()
        except EOFError:
            logger.warning("HITLReviewer: stdin closed (EOFError) — skipping remaining proposals")
            return "skip"
        except KeyboardInterrupt:
            logger.warning("HITLReviewer: operator pressed Ctrl+C — skipping remaining proposals")
            return "skip"
        if raw in ("skip", "s", "n", "no"):
            return "skip"
        if raw in ("auto", "a"):
            return "auto"
        return "yes"
