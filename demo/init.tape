# Adding doneguard to an existing project. Record from the repo root: vhs demo/init.tape
Output demo/init.gif
Require npm
Require docker
Source demo/style.tape
Set Height 560

Hide
Type "source demo/setup.sh store >/dev/null 2>&1 && clear"
Enter
Wait@5m
Show

Type "# An existing project: TypeScript, Postgres, Playwright"
Enter
Type "ls"
Enter
Sleep 1.5s
Type "doneguard init"
Enter
Wait
Sleep 2s
Type "# Commit .doneguard.conf so your team and CI share the checks"
Enter
Sleep 4s
