import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# ==============================================================================
# DECISION TYPES
# ==============================================================================


class Action(Enum):
    SNAPSHOT = "snapshot"
    ROLLBACK = "rollback"
    NOOP = "noop"


@dataclass
class Decision:
    action: Action
    checkpoint: str | None
    reason: str


# ==============================================================================
# SIGNAL CLASSIFIERS
# ==============================================================================

# Patterns that mean: something is broken, rollback
FAILURE_PATTERNS = [
    r"segmentation fault",
    r"core dumped",
    r"panic:",  # Go / Rust panics
    r"FATAL",
    r"out of memory",
    r"killed",  # OOM killer
    r"permission denied",
    r"no such file or directory",
    r"syntax error",
    r"traceback \(most recent call last\)",  # Python uncaught exception
    r"error\[E\d+\]",  # Rust compiler errors
    r"npm err!",
    r"build failed",
    r"test.*failed",
    r"assertion.*failed",
]

# Patterns that mean: things look good, commit the checkpoint
SUCCESS_PATTERNS = [
    r"tests? passed",
    r"build succeeded",
    r"all checks passed",
    r"\d+ passed",  # pytest summary line
    r"done\.",
    r"successfully",
]

_FAILURE_RE = re.compile("|".join(FAILURE_PATTERNS), re.IGNORECASE)
_SUCCESS_RE = re.compile("|".join(SUCCESS_PATTERNS), re.IGNORECASE)


def classify_exit(exit_code: int, stdout: str, stderr: str) -> Decision:
    """
    Primary signal: exit code.
    Secondary signal: pattern match on output when exit code is ambiguous.
    Returns a Decision with no model involved.
    """
    combined = f"{stdout}\n{stderr}"

    # Exit code is the ground truth — trust it first
    if exit_code == 0:
        if _FAILURE_RE.search(combined):
            # Exited clean but output looks wrong (e.g. test runner that never fails)
            return Decision(
                action=Action.ROLLBACK,
                checkpoint=None,
                reason=f"Exit 0 but failure pattern detected in output",
            )
        return Decision(
            action=Action.NOOP,
            checkpoint=None,
            reason="Clean exit, no failure patterns",
        )

    # Non-zero exit
    failure_match = _FAILURE_RE.search(combined)
    reason = failure_match.group(0) if failure_match else f"exit code {exit_code}"
    return Decision(action=Action.ROLLBACK, checkpoint=None, reason=reason)


# ==============================================================================
# SUPERVISOR LOOP
# ==============================================================================


class Supervisor:
    def __init__(self, engine):
        """
        engine: AgentStateEngine instance from orchestrator.py
        """
        self.engine = engine
        self._checkpoint_stack: list[str] = []

    def before_tool(self, tool_name: str) -> str:
        """Call before each tool execution. Returns the checkpoint name."""
        import time

        checkpoint = f"{tool_name}_{int(time.time())}"
        self.engine.create_checkpoint(checkpoint)
        self._checkpoint_stack.append(checkpoint)
        print(f"📸 [Supervisor] Checkpoint before '{tool_name}': {checkpoint}")
        return checkpoint

    def after_tool(
        self, tool_name: str, exit_code: int, stdout: str, stderr: str
    ) -> Decision:
        """Call after each tool execution. Handles rollback or commit automatically."""
        decision = classify_exit(exit_code, stdout, stderr)
        checkpoint = self._checkpoint_stack[-1] if self._checkpoint_stack else None

        if decision.action == Action.ROLLBACK:
            if checkpoint:
                print(
                    f"⏪ [Supervisor] Rolling back '{tool_name}' — reason: {decision.reason}"
                )
                self.engine.rollback_to(checkpoint)
                self._checkpoint_stack.pop()
            else:
                print(f"⚠️  [Supervisor] Rollback requested but no checkpoint available")

        elif decision.action == Action.NOOP:
            # Clean run — discard the checkpoint, keep progress
            if checkpoint:
                print(f"✅ [Supervisor] '{tool_name}' clean — discarding checkpoint")
                self.engine.delete_checkpoint(checkpoint)
                self._checkpoint_stack.pop()

        decision.checkpoint = checkpoint
        return decision

    def run_tool(
        self, tool_name: str, cmd: list[str], cwd: str | None = None
    ) -> Decision:
        """
        Convenience wrapper: checkpoint → run → evaluate → keep or rollback.
        This is the full loop in one call.
        """
        self.before_tool(tool_name)

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)

        return self.after_tool(
            tool_name,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


# ==============================================================================
# EXAMPLE USAGE
# ==============================================================================

if __name__ == "__main__":
    from pathlib import Path

    # Import your existing engine
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from orchestrator import AgentStateEngine

    engine = AgentStateEngine(
        workspace=Path("/mnt/agent-workspaces/active"),
        snapshot_dir=Path("/mnt/agent-workspaces/snapshots"),
    )

    supervisor = Supervisor(engine)

    # The full loop is now one line per tool call
    d = supervisor.run_tool("pytest", ["pytest", "tests/", "-q"])
    print(f"Decision: {d.action.value} — {d.reason}")

    d = supervisor.run_tool(
        "npm_build", ["npm", "run", "build"], cwd="/mnt/agent-workspaces/active/app"
    )
    print(f"Decision: {d.action.value} — {d.reason}")

    # If you still want human/agent natural language input, regex handles it
    # before you even touch a model:
    INTENT_PATTERNS = {
        Action.SNAPSHOT: re.compile(
            r"(snapshot|checkpoint|save|backup)", re.IGNORECASE
        ),
        Action.ROLLBACK: re.compile(r"(rollback|revert|undo|restore)", re.IGNORECASE),
    }

    def parse_intent(text: str, checkpoint_name: str) -> Decision:
        for action, pattern in INTENT_PATTERNS.items():
            if pattern.search(text):
                return Decision(
                    action=action, checkpoint=checkpoint_name, reason="regex match"
                )
        return Decision(action=Action.NOOP, checkpoint=None, reason="no match")

    print(parse_intent("revert to pre_run_alpha", "pre_run_alpha"))
    print(parse_intent("save a checkpoint called base_state", "base_state"))
