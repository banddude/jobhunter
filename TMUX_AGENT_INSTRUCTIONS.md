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

## Messaging other agents (peer to peer)

You can also send messages directly to the other agent (not just the coordinator). This is useful when:
- You need to coordinate on shared files (e.g. both editing server.py or ui-prototype.html)
- You finished something the other agent depends on
- You want to warn the other agent about a change that affects their work

Use the exact same process: list panes, capture the target pane to confirm which agent is there, then send with two separate commands.

## Team roster

| Pane | Name | Role |
|------|------|------|
| %24 | Frontend Designer | Builds and owns all UI in ui-prototype.html (views, wizard, dashboard) |
| %26 | Backend Architect | Builds and owns server.py, API endpoints, DB, pipeline execution |
| %2 | UX Engineer | Owns UX polish: toasts, loading states, validation, empty states, confirm dialogs |
| %27 | Coordinator (Aiva) | Assigns tasks, reviews work, manages the team |

## Message format

EVERY message you send MUST start with your role name AND your pane ID. This is mandatory so the recipient always knows who sent the message and where to reply. Format:

```
Frontend Designer (%24): done with dashboard polling, moving to onboarding wizard.
Backend Architect (%26): heads up, I changed the /api/logs endpoint in server.py.
UX Engineer (%2): toast system is wired, avoiding your loadDashboard section.
```

Before your first message, find your own pane ID by checking `tmux list-panes`. Your pane ID is the one running your process.

## Rules

1. ALWAYS list panes and confirm who is where before sending ANY message
2. NEVER send to a pane ID from memory without checking first
3. NEVER assume pane assignments are static, they change
4. If you cannot find or reach the coordinator, just print your status locally
5. Keep notification messages short, ALWAYS starting with your name and pane ID
6. You can message ANY pane (coordinator or other agents), just always verify first
7. When editing shared files, try to coordinate with the other agents to avoid merge conflicts
