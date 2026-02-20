# Tmux Agent Communication Rules

## CRITICAL: Identify agents before sending ANY message

Before sending ANY tmux message, you MUST:

1. **List all panes and see what's running in each one:**

```bash
tmux list-panes -a -F '#{pane_id} #{pane_title} #{pane_current_command} #{window_name}'
```

2. **Read the last few lines of the target pane to confirm the right agent is there:**

```bash
tmux capture-pane -t %TARGET -p -S -5
```

3. **Only after confirming the correct agent is in the pane, send your message.**

NEVER assume a pane ID is correct from memory. Panes get moved, closed, and recreated. Always look first, confirm, then send.

If the target pane does NOT exist or the wrong agent is in it, DO NOT send. Just print your status to your own stdout instead. The coordinator will check on you.

## Sending notifications to the coordinator

When you finish a task and need to notify the coordinator, you MUST use TWO separate bash commands:

```bash
# Step 1: Send the message text
tmux send-keys -t %COORDINATOR_PANE 'Your message here.'

# Step 2: Send Enter SEPARATELY
tmux send-keys -t %COORDINATOR_PANE Enter
```

NEVER combine text and Enter in a single send-keys call. They must always be two separate commands.

## Why two separate commands

Combining text and Enter in one call can cause the message to be pasted but not submitted, or worse, submit partial/garbled text. Sending them separately ensures the text lands in the input first, then Enter submits it cleanly.

## Rules

1. ALWAYS list panes and confirm who is where before sending ANY message
2. NEVER send to a pane ID from memory without checking first
3. NEVER assume pane assignments are static, they change
4. If you cannot find or reach the coordinator, just print your status locally
5. Keep notification messages short and prefixed with your role, e.g. "Codex1 done: description of what you did"
