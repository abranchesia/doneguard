# A turn where everything passes: Claude Code shows doneguard's receipt. Record from the repo root: vhs demo/receipt.tape
# Needs Claude Code logged in and Docker running.
Output demo/receipt.gif
Require claude
Require docker
Source demo/style.tape
Set FontSize 15
Set Height 620
Set Framerate 15

Hide
Type "source demo/setup.sh store >/dev/null 2>&1 && doneguard init >/dev/null && unset $(env | grep -oE '^(CLAUDE|ANTHROPIC)[A-Z_]*') && alias claude='command claude --setting-sources project,local --strict-mcp-config' && clear"
Enter
Wait@5m
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
Type "Add a one-line comment above total() in src/cart.ts saying what it returns."
Sleep 500ms
Enter
Wait+Screen@4m /doneguard: ✓/
Sleep 5s
