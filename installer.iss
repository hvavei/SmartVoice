#ifndef SourceDir
  #error SourceDir must point to the clean PyInstaller application directory
#endif
#ifndef OutputDir
  #define OutputDir "release"
#endif
#ifndef Edition
  #define Edition "Full"
#endif

[Setup]
AppId={{641F85CB-34CC-42EF-A038-6635FC6EA090}
AppName=SmartVoice
AppVersion=3.1.0
AppPublisher=SmartVoice
AppPublisherURL=https://github.com/hvavei/SmartVoice
AppSupportURL=https://github.com/hvavei/SmartVoice/issues
AppUpdatesURL=https://github.com/hvavei/SmartVoice/releases
DefaultDirName={localappdata}\Programs\SmartVoice
DefaultGroupName=SmartVoice
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=SmartVoice-Setup-{#Edition}
SetupIconFile=assets\smartvoice.ico
WizardImageFile=assets\wizard-image.bmp
WizardSmallImageFile=assets\wizard-small.bmp
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SmartVoice.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
CreateUninstallRegKey=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\SmartVoice.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\Remove-Legacy-SmartVoice.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\SmartVoice"; Filename: "{app}\SmartVoice.exe"; WorkingDir: "{app}"
Name: "{group}\卸载 SmartVoice"; Filename: "{uninstallexe}"; WorkingDir: "{app}"
Name: "{autodesktop}\SmartVoice"; Filename: "{app}\SmartVoice.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\SmartVoice.exe"; Description: "Launch SmartVoice"; Flags: nowait postinstall skipifsilent

; Only packaged program files are tracked for uninstall. User config/output/cache are not deleted.
