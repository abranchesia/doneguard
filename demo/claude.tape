# A real Claude Code session with doneguard, Postgres and Playwright. Record from the repo root: vhs demo/claude.tape
# Needs Claude Code logged in and Docker running. The alias keeps your own settings, plugins and MCP servers out
# of the recording.
Output demo/claude.gif
Require claude
Require npm
Require docker
Source demo/style.tape
Set FontSize 15
Set Height 900
Set Framerate 15

Hide
Type "source demo/setup.sh store >/dev/null 2>&1 && doneguard init >/dev/null && unset $(env | grep -oE '^(CLAUDE|ANTHROPIC)[A-Z_]*') && alias claude='command claude --setting-sources project,local --strict-mcp-config' && clear"
Enter
Wait@5m
# First run in this folder: accept the "trust this folder" prompt off camera.
Type "claude"
Enter
Sleep 6s
Down
Enter
Sleep 5s
Type "/exit"
Enter
Sleep 2s
Type "clear"
Enter
Show

Type "claude --permission-mode acceptEdits --tools Read,Edit,Write"
Enter
Sleep 5s
Type `Quick one, just edit src/cart.ts: make total() return the amount as a formatted string like "$20.00".`
Sleep 500ms
Enter
Wait+Screen@6m /· done/
Sleep 6s
Type "/exit"
Enter
Sleep 2s
Type "doneguard stats"
Enter
Sleep 6s
