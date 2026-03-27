---
name: server-codex
description: Submit a long-running task to Codex on remote server. This is an asynchronous tool. You must follow the 'Delegate and Forget' protocol.
---

# Server Codex

Control the Codex running on the remote server to execute programming and automation tasks.

## Overview

This skill enables you to delegate tasks to a Codex instance running on a remote server. The workflow is fully automated:

1. **Submit task** → Use `run_codex_on_server` tool with a prompt
2. **Wait** → Automatic polling runs in background (60s interval)
3. **Receive result** → Message bus delivers completion notification

**You only need to submit tasks and wait for results. No manual polling or file reading required.**

## Tool

### run_codex_on_server

Submit a task to the remote Codex instance.

**Parameters:**
- `prompts` (string, required): Task description. Be specific and include:
  - File paths (absolute or relative to `${HOME}`)
  - Expected output or behavior
  - Error messages if debugging

**Returns:**
```
Task submitted.
- task_dir: /path/to/task/directory
- Polling every 1 minutes automatically.
  You don't need to manually invoke 'read_file' or 'check_status' for this task.
  I will proactively notify you once the execution finishes or hits an error.
```

## Completion Notification

When the task finishes, you will receive a message via the message bus:

```
[CODEX_DONE] Task completed
task_dir: /path/to/task/directory
Output:
<codex output here>
```

**Metadata:**
- `source`: "codex"
- `task_dir`: task directory path
- `event`: "codex_done"

## Workflow

```
┌─────────────────┐
│  Submit task    │  ← run_codex_on_server(prompts="...")
│  with prompt    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Automatic      │  ← Background polling (60s interval)
│  polling starts │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Wait for       │  ← No action needed from you
│  completion     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Receive result  │  ← Message bus notification
│  via message    │
└─────────────────┘
```

## Examples

### Create a Python file
```
Use run_codex_on_server with:
prompts: "Create a hello_world.py file in the home directory that prints 'Hello World'"
```

### Debug an error
```
Use run_codex_on_server with:
prompts: "Fix the error in ~/project/main.py. The error is: FileNotFoundError: config.json not found. Create the missing config file with default settings."
```

### Run a shell script
```
Use run_codex_on_server with:
prompts: "Create and execute a shell script in ~/ that backs up the ~/data directory to ~/backup with timestamp"
```

### Complex task
```
Use run_codex_on_server with:
prompts: "Set up a Python virtual environment in ~/myproject, install requirements from ~/myproject/requirements.txt, and run main.py"
```

## Capabilities

The remote Codex can handle:
- **Programming tasks**: Python, Shell, JavaScript, etc.
- **File operations**: Create, edit, read, delete files
- **System operations**: Run commands, manage processes
- **Debugging**: Analyze errors and fix issues
- **Automation**: Orchestrate multi-step workflows

## Notes

- **Working directory**: Codex defaults to `${HOME}` on the server
- **No manual polling**: The polling function handles status checking automatically
- **Error handling**: Errors are reported through the message bus, same as success
- **One task at a time**: Submit tasks sequentially for clarity

## Do's and Don'ts

### ✅ Do
- Write clear, specific prompts
- Include file paths and expected outcomes
- Wait for the completion notification before submitting new tasks
- Trust the automated polling process

### ❌ Don't
- Manually read `status.txt` or `codex.out`
- Invoke `read_file` to check task progress
- Poll the task directory yourself
- Worry about the polling mechanism
