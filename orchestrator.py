import os
import subprocess
import time
from pathlib import Path

WORKSPACE_PATH = Path("/mnt/agent-workspaces")
SNAPSHOT_PATH = Path("/mnt/snapshots")


class AgentStateEngine:
    def __init__(self, workspace: Path, snapshot_dir: Path):
        self.workspace = workspace
        self.snapshot_dir = snapshot_dir

    def create_checkpoint(self, checkpoint_name: str) -> Path:
        """Takes a microsecond Btrfs Copy-on-Write snapshot of the workspace."""
        target = self.snapshot_dir / checkpoint_name
        if target.exists():
            # Clean up old tracking reference if overwritten
            self.delete_checkpoint(checkpoint_name)

        print(f"📸 [Btrfs] Creating checkpoint: {checkpoint_name}...")
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", str(self.workspace), str(target)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return target

    def rollback_to(self, checkpoint_name: str):
        """Swaps the B-tree pointers back to a pristine snapshot state."""
        target = self.snapshot_dir / checkpoint_name
        if not target.exists():
            raise FileNotFoundError(f"Checkpoint {checkpoint_name} not found.")

        print(f"⏪ [Btrfs] Rolling back workspace to checkpoint: {checkpoint_name}...")

        # Atomically delete the corrupted workspace and restore from checkpoint
        subprocess.run(
            ["btrfs", "subvolume", "delete", str(self.workspace)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", str(target), str(self.workspace)],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def fork_branch(self, base_checkpoint: str, branch_name: str) -> Path:
        """Forks a parallel testing branch out of an existing checkpoint."""
        source = self.snapshot_dir / base_checkpoint
        branch_path = self.workspace.parent / branch_name
        print(f"🌿 [Btrfs] Forking branch '{branch_name}' from '{base_checkpoint}'...")
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", str(source), str(branch_path)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return branch_path

    def delete_checkpoint(self, checkpoint_name: str):
        """Clears a checkpoint out of the ledger."""
        target = self.snapshot_dir / checkpoint_name
        if target.exists():
            subprocess.run(
                ["btrfs", "subvolume", "delete", str(target)],
                check=True,
                stdout=subprocess.DEVNULL,
            )


# ==============================================================================
# Example Agent Tool Execution Loop With Safe Failure Traps
# ==============================================================================
if __name__ == "__main__":
    # Initialize the engine
    engine = AgentStateEngine(WORKSPACE_PATH, SNAPSHOT_PATH)

    # 1. Establish our baseline checkpoint before the agent acts
    checkpoint_id = f"pre_tool_{int(time.time())}"
    engine.create_checkpoint(checkpoint_id)

    try:
        print("\n🤖 Agent starts editing files inside the sandbox...")
        # Simulating an agent editing a file inside the workspace
        test_file = WORKSPACE_PATH / "config.json"
        with open(test_file, "w") as f:
            f.write("{ 'invalid_json': True, 'corrupted': yes }")  # Malformed content

        print("⚡ Executing agent container verification tool...")
        # Simulate running a validation tool inside Podman sitting on top of Btrfs
        # In a real tool call, this would execute your actual container stack
        result = subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "-v",
                f"{WORKSPACE_PATH}:/app:ro",
                "alpine",
                "json_verify_mock_fail",
            ],
            capture_output=True,
            text=True,
        )

        # Force an explicit failure block if verification fails
        if result.returncode != 0:
            raise RuntimeError("Agent tool execution introduced a syntax regression.")

    except Exception as e:
        print(f"❌ Danger Identified: {e}")
        # The workspace is poisoned. Trigger an instant rollback.
        start_time = time.time()
        engine.rollback_to(checkpoint_id)
        print(
            f"⏱️ Environment completely unpoisoned in {(time.time() - start_time) * 1000:.2f}ms!"
        )

        # Clean up the staging checkpoint
        engine.delete_checkpoint(checkpoint_id)
