' Runs a command with no visible window (scheduled tasks otherwise pop a console
' and steal focus, e.g. from a full-screen game). Usage:
'   wscript.exe run_hidden.vbs "<program>" [args...]
Dim sh, cmd, i
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  cmd = cmd & """" & WScript.Arguments(i) & """ "
Next
WScript.Quit sh.Run(Trim(cmd), 0, True)
