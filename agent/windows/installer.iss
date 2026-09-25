; QCBridge Agent — Windows installer (Inno Setup 6), unsigned by decision
; (2026-09-24): no Windows certificate; SmartScreen will warn once.
; Same shape as MinRender's installer/minrender_installer.iss.
;
; Build on the Windows box after agent\build-win.ps1:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" agent\windows\installer.iss
; → agent\dist\QCBridge-Agent-<version>-Setup-x64.exe
#define MyAppName "QCBridge Agent"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "cbkow"
#define MyAppURL "https://github.com/cbkow/QCBridge"
#define MyAppExeName "qcbridge-agent.exe"

[Setup]
AppId={{7C1E2B8A-5F4D-4B0E-9C33-2A6D1F0E8B41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright=Copyright (C) 2026 {#MyAppPublisher}
DefaultDirName={autopf}\QCBridge
DefaultGroupName=QCBridge
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\dist
OutputBaseFilename=QCBridge-Agent-{#MyAppVersion}-Setup-x64
Compression=lzma2/max
SolidCompression=yes
SetupIconFile=..\assets\icons\qcbridge.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
; No autostart (owner's decision): the agent is started from the Start
; menu, or by the Blender extension when a session needs it.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\target\release\qcbridge-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\target\release\qcb-capture-win.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "The QC Bridge tray agent"
Name: "{group}\{#MyAppName} Settings"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--settings"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; A running agent holds its exe; stop it before the files go. Its job
; object takes Blender, the capture helper and ffmpeg with it.
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM qcbridge-agent.exe"; Flags: runhidden; RunOnceId: "StopAgent"

[Code]
// Stop a running agent before the files are replaced (an update).
procedure CurStepChanged(CurStep: TSetupStep);
var
  R: Integer;
begin
  if CurStep = ssInstall then
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM qcbridge-agent.exe', '', SW_HIDE, ewWaitUntilTerminated, R);
end;
