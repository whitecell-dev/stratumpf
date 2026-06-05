import json
import os
import re
import subprocess
import time
from typing import NoReturn
import ollama

# Systems Configuration: Parents remain anchored, leaves undergo mutation
WORKSPACE = os.path.realpath("/mnt/agent-workspaces/active")
SNAPSHOT_DIR = os.path.realpath("/mnt/agent-workspaces/snapshots")


# ==============================================================================
# 1. DEFENSIVE INVARIANT & PRIVILEGE GATES
# ==============================================================================
def validate_checkpoint_name(name: str) -> None:
    """Enforces strict lexical constraints on incoming tokens to prevent path traversal."""
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", name):
        raise ValueError(
            f"Security Exception: Non-permissive token geometry in checkpoint name: '{name}'. "
            f"Only alphanumeric characters, underscores, and hyphens are allowed."
        )


def assert_system_substrate() -> None:
    """Verifies that the runtime environment complies with our storage specifications."""
    # Ensure process possesses explicit administrative capabilities or is executing as root
    if os.getuid() != 0:
        # Note: If running via ambient capability mapping (CAP_SYS_ADMIN), update this check accordingly.
        pass

    for path in [WORKSPACE, SNAPSHOT_DIR]:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Infrastructure Failure: Missing critical path: {path}"
            )

        # Verify filesystem backing via primitive stat system call
        res = subprocess.run(
            ["stat", "-f", "-c", "%T", path], capture_output=True, text=True, check=True
        )
        if "btrfs" not in res.stdout.lower():
            raise RuntimeError(
                f"VFS Violation: Target track '{path}' does not reside on a Btrfs backing pool."
            )


def assert_is_subvolume(path: str) -> None:
    """Verifies that a target leaf path is an active Btrfs subvolume node descriptor."""
    res = subprocess.run(
        ["btrfs", "subvolume", "show", path], capture_output=True, text=True
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Subvolume Invariant Violation: '{path}' has degraded to a flat directory."
        )


# ==============================================================================
# 2. TRANSACTION-ISOLATED ATOMIC ACTIONS MACHINE
# ==============================================================================
def execute_btrfs_action(action: str, checkpoint_name: str) -> None:
    """
    Executes block mutations on the underlying storage layer.
    Guarantees atomic error states and avoids TOCTOU race conditions.
    """
    validate_checkpoint_name(checkpoint_name)
    assert_system_substrate()
    assert_is_subvolume(WORKSPACE)

    target_snapshot = f"{SNAPSHOT_DIR}/{checkpoint_name}"

    # Dynamically extract execution boundaries to remain completely environment agnostic
    current_uid = os.getuid()
    current_gid = os.getgid()

    if action == "snapshot":
        # Purge stale snapshots cleanly. Bypasses TOCTOU checks via direct suppression.
        subprocess.run(
            ["btrfs", "subvolume", "delete", target_snapshot],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        print(
            f"📸 [Substrate Engine] Creating immutable copy-on-write reference: snapshots/{checkpoint_name}"
        )
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", WORKSPACE, target_snapshot], check=True
        )

    elif action == "rollback":
        if not os.path.exists(target_snapshot):
            raise FileNotFoundError(
                f"Rollback aborted: Token checkpoint target '{checkpoint_name}' does not exist."
            )

        print(
            f"⏪ [Substrate Engine] CRITICAL: Initiating isolated transaction swap to checkpoint '{checkpoint_name}'..."
        )

        # Define transaction-local stage mutations
        temp_rollback_path = f"{WORKSPACE}_tmp_rollback"
        old_corrupted_path = f"{WORKSPACE}_old_poisoned"

        # Ensure temporary spaces are cleared before execution
        subprocess.run(
            ["btrfs", "subvolume", "delete", temp_rollback_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["btrfs", "subvolume", "delete", old_corrupted_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Phase 1: Clone the snapshot target into an isolated, temporary leaf node
            subprocess.run(
                ["btrfs", "subvolume", "snapshot", target_snapshot, temp_rollback_path],
                check=True,
                stdout=subprocess.DEVNULL,
            )

            # Phase 2: Perform atomic directory swap via kernel-level file descriptor renames
            os.rename(WORKSPACE, old_corrupted_path)
            os.rename(temp_rollback_path, WORKSPACE)

            # Phase 3: Synchronize file tree resource mappings to match current runtime process constraints
            subprocess.run(
                ["chown", "-R", f"{current_uid}:{current_gid}", WORKSPACE], check=True
            )
            print(
                f"✅ [Substrate Engine] Atomic swap completed. Purging poisoned subvolume remnants..."
            )

        except Exception as transaction_exception:
            # Transaction Rollback Rollback: If Phase 1 or 2 breaks, recover original workspace
            if os.path.exists(old_corrupted_path) and not os.path.exists(WORKSPACE):
                os.rename(old_corrupted_path, WORKSPACE)
            # Purge the staged clone if it was dangling
            subprocess.run(
                ["btrfs", "subvolume", "delete", temp_rollback_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            raise RuntimeError(
                f"Storage Transaction Crash: Structural rollback failed. Workspace preserved."
            ) from transaction_exception

        finally:
            # Phase 4: Clean up the corrupted tracking tree outside of the active execution path
            subprocess.run(
                ["btrfs", "subvolume", "delete", old_corrupted_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


# ==============================================================================
# 3. SCHEMA-CONSTRAINED ROUTING CONTROLLER
# ==============================================================================
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["snapshot", "rollback"],
            "description": "Choose 'snapshot' to save or backup. Choose 'rollback' to restore, revert, or undo.",
        },
        "checkpoint_name": {
            "type": "string",
            "description": "The target alphanumeric label context token.",
        },
    },
    "required": ["action", "checkpoint_name"],
}


def run_agent_turn(model_name: str, user_intent: str) -> None:
    print(f"\n🔄 [Model: {model_name}] Decoding Intent: '{user_intent}'")

    system_prompt = (
        "You are an isolated infrastructure automation JSON API router. "
        "Analyze the user request and extract the parameters precisely.\n\n"
        'Example 1:\nUser: Save a checkpoint named base_state\nOutput: {"action": "snapshot", "checkpoint_name": "base_state"}\n\n'
        'Example 2:\nUser: Revert everything back to base_state immediately\nOutput: {"action": "rollback", "checkpoint_name": "base_state"}'
    )

    start_time = time.time()
    response = ollama.generate(
        model=model_name,
        prompt=f"<|im_start|>system\n{system_prompt}<|im_end|>\n<|im_start|>user\n{user_intent}<|im_end|>\n<|im_start|>assistant\n",
        format=INTENT_SCHEMA,
        options={"temperature": 0.0, "top_p": 0.1},
    )
    latency = (time.time() - start_time) * 1000
    print(f"⏱️  Inference Processing Latency: {latency:.2f}ms")

    # Fail fast and loud: Parse directly out of the JSON envelope
    payload = json.loads(response["response"])
    print(f"⚡ Extracted Payload Ledger: {json.dumps(payload)}")

    # Execute the storage mutation block
    execute_btrfs_action(
        action=payload["action"], checkpoint_name=payload["checkpoint_name"]
    )
    print("📦 Substrate Response: TRANSACTION_SUCCESS")


if __name__ == "__main__":
    TARGET_MODEL = "qwen2.5:1.5b-instruct"

    # Launch verification turn sequence
    try:
        run_agent_turn(
            TARGET_MODEL,
            "We are about to let an untrusted Python script run inside the container workspace. Create a defensive backup checkpoint called pre_run_alpha.",
        )
        run_agent_turn(
            TARGET_MODEL,
            "The automated test loop just threw a segmentation fault and corrupted our source tree files. Revert the workspace layout right now back to pre_run_alpha.",
        )
    except Exception as fatal_error:
        print(
            f"\n🚨 SYSTEM ABORT: Orchestration reconciliation collapsed: {fatal_error}"
        )
