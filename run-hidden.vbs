' Lanza el panel del bot (app.py) SIN ventana de consola. Task Scheduler llama a
' este script (via wscript.exe) en vez de cmd.exe, para que no quede una consola
' abierta en el escritorio mientras el bot corre.
'
' Uso:  wscript.exe run-hidden.vbs [ruta\a\python.exe]
'       (sin argumento usa "python" del PATH)
'
' Trabaja en la carpeta del propio script, redirige stdout/stderr a bot.out.log /
' bot.err.log (igual que hacia la tarea original con cmd /c) y espera al proceso
' propagando su exit code, para que Task Scheduler siga viendo el estado real.
Dim fso, shell, base, py, cmd, code
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = base
If WScript.Arguments.Count > 0 Then py = WScript.Arguments(0) Else py = "python"
cmd = "cmd.exe /c """"" & py & """ -u app.py > bot.out.log 2> bot.err.log"""
code = shell.Run(cmd, 0, True)
WScript.Quit code
