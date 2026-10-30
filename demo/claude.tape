# A real Claude Code session with doneguard. Record from the repo root: vhs demo/claude.tape
# Needs Claude Code logged in. The alias keeps your own settings, plugins and MCP servers out of the
# recording. First run only: answer the "trust this folder" prompt for /tmp/doneguard-demo/shop once.
Output demo/claude.gif
Require claude
Require npm
Source demo/style.tape
Set FontSize 15
Set Height 860
Set Framerate 15

Hide
Type "source demo/setup.sh shop && doneguard init >/dev/null && unset $(env | grep -oE '^(CLAUDE|ANTHROPIC)[A-Z_]*') && alias claude='command claude --setting-sources project,local --strict-mcp-config' && clear"
Enter
Wait
Show

Type "claude --permission-mode acceptEdits --tools Read,Edit,Write"
Enter
Sleep 5s
Type `Quick one, just edit src/cart.ts: make total() return the amount as a formatted string like "$20.00".`
Sleep 500ms
Enter
Wait+Screen@4m /· done/
Sleep 5s
Type "/exit"
Enter
Sleep 2s
Type "doneguard stats"
Enter
Sleep 6s
