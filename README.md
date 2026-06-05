# agent-state-engine

**Deterministic agent execution. Instant rollback. Branchable workspaces.**

Turn your filesystem into a snapshot-based, rollback-capable execution substrate for AI agents. This treats the filesystem as a transactional state machine — tool calls are transitions, snapshots are savepoints, branches are parallel worlds. Built on Btrfs subvolume snapshots, it gives your agent the ability to:

- **Checkpoint** before any risky tool call (~15ms)
- **Rollback** instantly when something breaks (~40ms)
- **Fork** parallel execution branches from any checkpoint
- **Recover** without re-sending context or re-syncing state

No more token waste on failed tool calls. No more "workspace poisoned, restart from scratch." Just deterministic, replayable agent execution.

---

## Why This Exists

Current agent architectures keep state in memory or loose JSON files. When a tool call fails, you're stuck:

- Re-sending entire conversation history (thousands of tokens)
- Manually unwinding side effects
- Restarting from a clean state, losing all progress

**Btrfs snapshots fix this at the block layer.**

Snapshot before the tool call → milliseconds. Tool fails → rollback to snapshot → milliseconds. Agent retries with the same context and a clean workspace. No token waste. No manual cleanup. No state leakage.

---

## Requirements

- **Linux** with a Btrfs-formatted volume (WSL2 works well — see setup below)
- **Python 3.8+**
- **btrfs-progs** (`apt install btrfs-progs`)
- **Podman** or Docker (for containerized tool execution, optional but recommended)

---

## Installation

```bash
pip install agent-state-engine
```

Or from source:

```bash
git clone https://github.com/yourusername/agent-state-engine
cd agent-state-engine
pip install -e .
```

---

## Quick Start

```python
from agent_state_engine import AgentStateEngine
from pathlib import Path

engine = AgentStateEngine(
    workspace=Path("/mnt/agent-workspaces/active"),
    snapshot_dir=Path("/mnt/agent-workspaces/snapshots")
)

engine.create_checkpoint("pre_tool_call")

print(os.listdir(engine.workspace))  # ['config.json', 'src/', 'data/']

try:
    run_risky_tool()  # corrupts config.json, deletes src/
except Exception:
    engine.rollback_to("pre_tool_call")

print(os.listdir(engine.workspace))  # ['config.json', 'src/', 'data/'] — identical
```

The workspace reverts completely. No manual cleanup, no re-sending context.

---

## API Reference

### `AgentStateEngine(workspace: Path, snapshot_dir: Path)`

Main class for managing agent state.

---

#### `create_checkpoint(name: str) -> Path`

Takes a Btrfs snapshot of the current workspace. Returns the snapshot path.

- **Time:** ~15ms (measured on WSL2 + NVMe SSD)
- **Space:** Copy-on-Write — near-zero overhead for unchanged files

If a checkpoint with the given name already exists, it is deleted and replaced.

---

#### `rollback_to(name: str)`

Replaces the current workspace with the named snapshot. From the agent's perspective this is atomic — no partial workspace state is ever exposed. Internally: the corrupted workspace is deleted, a new subvolume is cloned from the checkpoint, and ownership is reset.

- **Time:** ~40ms (measured on WSL2 + NVMe SSD)
- **Effect:** Workspace reverts to exact snapshot state

Raises `FileNotFoundError` if the checkpoint does not exist.

---

#### `fork_branch(base_checkpoint: str, branch_name: str) -> Path`

Creates a new independent workspace from an existing checkpoint. Useful for running parallel agent strategies without interference.

```python
branch_a = engine.fork_branch("pre_tool_call", "strategy-aggressive")
branch_b = engine.fork_branch("pre_tool_call", "strategy-conservative")
```

The returned path is `workspace.parent / branch_name`.

---

#### `delete_checkpoint(name: str)`

Removes a checkpoint and frees its space. No-op if the checkpoint does not exist.

---

## The Full Agent Loop

```python
engine = AgentStateEngine(workspace, snapshot_dir)

while tasks_remaining:
    task_id = get_next_task_id()
    checkpoint = engine.create_checkpoint(f"task_{task_id}")

    success = agent.execute_next_tool()

    if not success:
        engine.rollback_to(checkpoint)
        # Retry with clean state — no token waste
        continue

    # Progress committed; discard the checkpoint
    engine.delete_checkpoint(checkpoint)
```

---

## Integration with Podman / Docker

```python
import subprocess
from pathlib import Path

def run_tool_in_container(
    workspace: Path,
    tool_image: str,
    command: str
) -> bool:
    result = subprocess.run([
        "podman", "run", "--rm",
        "-v", f"{workspace}:/workspace:rw",
        tool_image,
        "bash", "-c", command
    ], capture_output=True)
    return result.returncode == 0
```

Wrap this in your agent's tool call, checkpoint before, rollback on failure.

---

## Branching Execution Example

```python
def explore_strategies(engine: AgentStateEngine, problem: str) -> dict:
    engine.create_checkpoint("problem_analysis")

    strategies = ["aggressive", "conservative", "creative"]
    results = {}

    for strategy in strategies:
        branch_path = engine.fork_branch("problem_analysis", f"strategy_{strategy}")

        # Each branch gets its own engine instance — no shared mutable state
        branch_engine = AgentStateEngine(branch_path, engine.snapshot_dir)

        try:
            results[strategy] = execute_strategy(problem, branch_engine)
        except Exception:
            results[strategy] = "FAILED"
        finally:
            branch_engine.delete_checkpoint(f"strategy_{strategy}")

    engine.delete_checkpoint("problem_analysis")
    return results
```

---

## Performance

| Operation | Time | Space |
|-----------|------|-------|
| Create snapshot | ~15ms | CoW — ~0 bytes for unchanged files |
| Rollback | ~40ms | N/A — delete + clone, no partial state exposed |
| Fork branch | ~15ms | CoW — shared blocks |

Measured on WSL2 + NVMe SSD (consumer hardware). All operations avoid exposing partial workspace state to the agent.

---

## Why Btrfs?

| Feature | Why it matters for agents |
|---------|--------------------------|
| Copy-on-Write snapshots | Instant checkpoints, near-zero space cost |
| Subvolumes | Isolated workspaces per agent or task |
| Compression (`zstd`) | 4–6× compression on logs and telemetry |
| Checksums | Detect corruption before it poisons state |
| `btrfs send/receive` | Sync snapshots between machines |

---

## Storage Layout

All state lives under a single mount root. The parent directory is a stable anchor; only the leaves mutate.

```
/mnt/agent-workspaces/
├── active/         # mutable workspace leaf  (@workspaces subvolume)
├── snapshots/      # rollback checkpoints    (@snapshots subvolume)
└── podman/         # container image layers  (@podman subvolume)
```

This layout is consistent across the codebase, cloud-init config, and WSL2 setup below. If you change it, update `WORKSPACE` and `SNAPSHOT_DIR` in `agent_substrate.py` and verify `os.rename` staging paths stay within the same subvolume tree.

---

## WSL2 Setup (Windows)

> **Note:** Device names (`/dev/sdX`) are assigned dynamically at boot and may change between sessions. The setup script below detects your disk by size rather than by name to avoid this.

```bash
# 1. Create and attach a dedicated VHDX in WSL2
#    (from PowerShell, then attach in wsl.conf or manually)

# 2. Identify the new disk by size (adjust 50G to match your VHDX)
DISK=$(lsblk -dpno NAME,SIZE | grep 50G | awk '{print $1}')

# 3. Format as Btrfs
sudo mkfs.btrfs -f -L agent-cow-store "$DISK"

# 4. Create subvolumes
sudo mount "$DISK" /mnt/btrfs-root
sudo btrfs subvolume create /mnt/btrfs-root/@workspaces
sudo btrfs subvolume create /mnt/btrfs-root/@snapshots
sudo btrfs subvolume create /mnt/btrfs-root/@podman
sudo umount /mnt/btrfs-root

# 5. Mount with compression
sudo mkdir -p /mnt/agent-workspaces
sudo mount -o compress=zstd:3,autodefrag,subvol=@workspaces "$DISK" /mnt/agent-workspaces
sudo mkdir -p /mnt/agent-workspaces/snapshots
sudo mount -o compress=zstd:3,subvol=@snapshots "$DISK" /mnt/agent-workspaces/snapshots
sudo mount -o compress=zstd:3,subvol=@podman    "$DISK" /mnt/agent-workspaces/podman

# 6. Run your agent
sudo .venv/bin/python3 agent_substrate.py
```

To persist mounts across reboots, add entries to `/etc/fstab` using the disk UUID (`blkid "$DISK"`) rather than the device name.

**Privilege options for Btrfs operations:**

- Run as root (simplest for development)
- Grant `CAP_SYS_ADMIN` to the process
- Delegate to a privileged helper service

---

## Automated First-Boot with cloud-init

The repository includes a complete `cloud-init` configuration (`cloud-init/agent-debian.yaml`) that:

- Installs `btrfs-progs`, Podman, and dependencies
- Formats a Btrfs volume on first boot (device detected by size, not hardcoded path)
- Creates `@workspaces`, `@podman`, and `@snapshots` subvolumes
- Configures rootless Podman with the Btrfs storage driver
- Persists mounts via `/etc/fstab`
- Enables systemd in WSL2

See [`cloud-init/agent-debian.yaml`](cloud-init/agent-debian.yaml) for the full configuration.

---

## Edge LLM Intent Routing

The repository includes `agent_substrate.py`, a working orchestration layer that pairs a local Small Language Model with the snapshot engine. Instead of hardcoding snapshot/rollback calls, natural language — from a human operator, an agent's log output, or a monitoring system — is routed directly into filesystem actions.

### How It Works

[Ollama](https://ollama.com) runs a `qwen2.5:1.5b-instruct` model locally at `temperature: 0.0` with constrained grammar decoding (`format=INTENT_SCHEMA`). Free-form text is converted into a typed JSON payload with zero variance:

```python
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["snapshot", "rollback"]
        },
        "checkpoint_name": {
            "type": "string"
        }
    },
    "required": ["action", "checkpoint_name"]
}
```

The model never outputs free text — only a valid action payload. That payload is sanitized, validated against the Btrfs substrate, and executed.

### Observed Performance

```
🔄 Decoding: 'Create a defensive backup checkpoint called pre_run_alpha.'
⏱️  Inference: 2360ms
⚡ Payload: {"action": "snapshot", "checkpoint_name": "pre_run_alpha"}
📦 TRANSACTION_SUCCESS

🔄 Decoding: 'The test loop corrupted our source tree. Revert to pre_run_alpha.'
⏱️  Inference: 2286ms
⚡ Payload: {"action": "rollback", "checkpoint_name": "pre_run_alpha"}
📦 TRANSACTION_SUCCESS
```

~2.3s consistent inference on a 1.5B model running fully local. No API calls, no cloud dependency.

### Requirements

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:1.5b-instruct

pip install ollama
```

### Running It

```bash
sudo .venv/bin/python3 agent_substrate.py
```

> **Note:** `sudo` is required for Btrfs subvolume operations. If running with ambient `CAP_SYS_ADMIN` capabilities instead of full root, update the privilege check in `assert_system_substrate()` accordingly. Using the explicit venv path preserves package lookups under sudo.

### Security Gates

Before any filesystem mutation, the controller enforces:

1. **Checkpoint name sanitization** — regex `^[a-zA-Z0-9_\-]+$` blocks path traversal
2. **Btrfs substrate check** — `stat` confirms both workspace and snapshot dir reside on Btrfs
3. **Subvolume validation** — confirms the workspace is a genuine subvolume, not a flat directory

### The Rollback Transaction

Rollbacks use a three-phase sequence to avoid leaving the workspace in a half-replaced state:

1. **Stage** — clone the target snapshot into a temporary leaf (`active_tmp_rollback`)
2. **Swap** — delete the corrupted workspace, rename the staged clone into position
3. **Purge** — delete the temporary staging path

If phase 1 or 2 fails, the original workspace is restored from `active_old_poisoned` before the exception propagates.

> **Known limitation:** `os.rename` across Btrfs subvolume boundaries raises `EXDEV`. The current implementation avoids this by staging within the same parent directory, but if you restructure the mount layout, verify the rename stays within one subvolume tree or replace it with a direct delete+snapshot sequence.

---

## Snapshot Hygiene

Snapshots accumulate. Add a cron job to prune checkpoints older than 2 days:

```bash
find /mnt/agent-workspaces/snapshots -maxdepth 1 -mindepth 1 -type d -mtime +2 \
  -exec btrfs subvolume delete {} \;
```

Or call `engine.delete_checkpoint(name)` explicitly in your agent loop once a task succeeds.

---

## Roadmap

- [ ] PyPI package
- [ ] CLI for snapshot inspection and manual rollback
- [ ] Podman runtime hooks — feed live container logs into the intent router
- [ ] Multi-subvolume state tracker — manage concurrent agent branches
- [ ] LangChain integration (auto-checkpoint before tool calls)
- [ ] AutoGen integration
- [ ] Remote snapshot sync via `btrfs send/receive` over SSH
- [ ] Configurable GC policy engine
- [ ] Expand intent schema to support `fork`, `delete`, and `list` actions

---

## Known Issues

| Issue | Status |
|-------|--------|
| `os.rename` fails with `EXDEV` if staging paths cross subvolume boundaries | Documented — avoid restructuring mount layout |
| `assert_system_substrate` privilege check is a stub | Raises on non-root; `CAP_SYS_ADMIN` path not yet implemented |
| No retry limit in agent loop — a persistent tool failure loops indefinitely | Caller's responsibility for now |
| Inference latency (~2.3s) blocks the agent loop synchronously | Async routing not yet implemented |

---

## Contributing

This is as much a research artifact as a library. Contributions welcome.

- Break it. Document how.
- Add snapshot strategies or GC policies.
- Integrate with your agent framework and share the adapter.
- Port the intent router to a different local model and report the results.

---

## License

MIT

---

> *"Your agent doesn't need to be perfect. Btrfs makes its mistakes free."*
