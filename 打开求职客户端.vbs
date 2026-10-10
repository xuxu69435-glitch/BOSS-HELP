Option Explicit

Dim shell, files, root, powershell, command, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
command = """" & powershell & """ -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & root & "\scripts\start_client.ps1"" -OpenBrowser"

result = 1
On Error Resume Next
result = shell.Run(command, 0, True)
If Err.Number <> 0 Then result = 1
On Error GoTo 0

If result <> 0 Then
    MsgBox "The BOSS client could not start. Check these local logs:" & vbCrLf & vbCrLf & _
        root & "\logs\client-launcher.log" & vbCrLf & _
        root & "\data\boss-agent\client-startup-error.log" & vbCrLf & _
        root & "\data\boss-agent\client-control\server.log" & vbCrLf & vbCrLf & _
        "If no log was created, check that scripts\start_client.ps1 exists and Windows Script Host / PowerShell is available.", _
        vbOKOnly + vbExclamation, "BOSS Job Client"
    WScript.Quit 1
End If
WScript.Quit 0
