# Starting a project from an empty folder. Record from the repo root: vhs demo/new.tape
Output demo/new.gif
Require npm
Source demo/style.tape
Set Height 560

Hide
Type "source demo/setup.sh empty && clear"
Enter
Wait
Show

Type "mkdir my-app && cd my-app"
Enter
Type "doneguard init"
Enter
Wait
Sleep 2.5s
Type "# Scaffold the project (you or the agent), then run init again:"
Enter
Type "npm init -y >/dev/null && npm i -D typescript vitest --silent && npx tsc --init >/dev/null"
Enter
Hide
Wait
Show
Type "doneguard init"
Enter
Wait
Sleep 4s
