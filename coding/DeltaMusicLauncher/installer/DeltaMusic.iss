; Optional Inno Setup 6 script for an unsigned DeltaMusic installer.
; Build the release ZIP/folder first, then compile this script with ISCC.exe.

#ifndef SourcePath
  #define SourcePath "..\release\DeltaMusic"
#endif

[Setup]
AppId={{5E895C13-8B75-4A31-99A7-50CBB8AC0A01}
AppName=DeltaMusic
AppVersion=1.0.0
AppPublisher=DeltaMusic Community
DefaultDirName={autopf}\DeltaMusic
DefaultGroupName=DeltaMusic
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release\installer
OutputBaseFilename=DeltaMusic-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=DeltaMusic

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourcePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DeltaMusic"; Filename: "{app}\Start_DeltaMusic.cmd"; WorkingDir: "{app}"
Name: "{autodesktop}\DeltaMusic"; Filename: "{app}\Start_DeltaMusic.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\Start_DeltaMusic.cmd"; Description: "Launch DeltaMusic"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

