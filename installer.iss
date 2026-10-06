#ifndef SourceDir
  #error SourceDir must point to the clean PyInstaller application directory
#endif
#ifndef OutputDir
  #define OutputDir "release"
#endif
#ifndef Edition
  #define Edition "Full"
#endif
#define CurrentVersion "3.1.0"

[Setup]
AppId={{641F85CB-34CC-42EF-A038-6635FC6EA090}
AppName=SmartVoice
AppVersion={#CurrentVersion}
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
AppMutex=SmartVoiceServerMutex
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

[InstallDelete]
; 跨版本清理：Full→Standard 直接覆盖时旧组件目录不会自动消失，安装开始即删；
; Full 版随后由 [Files] 重装，Standard 版保持干净（只删已知组件目录，不碰用户数据）。
Type: filesandordirs; Name: "{app}\_internal\numpy"
Type: filesandordirs; Name: "{app}\_internal\numpy.libs"
Type: filesandordirs; Name: "{app}\_internal\onnxruntime"
Type: filesandordirs; Name: "{app}\_internal\tokenizers"
Type: filesandordirs; Name: "{app}\_internal\cv2"
Type: filesandordirs; Name: "{app}\_internal\rapidocr_onnxruntime"
Type: filesandordirs; Name: "{app}\_internal\shapely"
Type: filesandordirs; Name: "{app}\_internal\shapely.libs"
Type: filesandordirs; Name: "{app}\_internal\pyclipper"
Type: filesandordirs; Name: "{app}\_internal\yaml"
Type: filesandordirs; Name: "{app}\models\g2pw"

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{641F85CB-34CC-42EF-A038-6635FC6EA090}_is1';

function CompareVersion(const A, B: String): Integer;
var
  S1, S2: String;
  P1, P2, N1, N2: Integer;
begin
  S1 := A;
  S2 := B;
  Result := 0;
  while (S1 <> '') or (S2 <> '') do
  begin
    P1 := Pos('.', S1);
    if P1 = 0 then begin N1 := StrToIntDef(S1, 0); S1 := ''; end
    else begin N1 := StrToIntDef(Copy(S1, 1, P1 - 1), 0); S1 := Copy(S1, P1 + 1, Length(S1)); end;
    P2 := Pos('.', S2);
    if P2 = 0 then begin N2 := StrToIntDef(S2, 0); S2 := ''; end
    else begin N2 := StrToIntDef(Copy(S2, 1, P2 - 1), 0); S2 := Copy(S2, P2 + 1, Length(S2)); end;
    if N1 < N2 then begin Result := -1; Exit; end;
    if N1 > N2 then begin Result := 1; Exit; end;
  end;
end;

function InitializeSetup(): Boolean;
var
  Installed: String;
begin
  Result := True;
  // 降级门控：已装版本更新时直接拒绝，不静默回退（旧代码 + 新用户数据语义错乱）。
  if RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Installed) then
    if CompareVersion(Installed, '{#CurrentVersion}') > 0 then
    begin
      MsgBox('检测到已安装更新版本 ' + Installed + '，当前安装包为 {#CurrentVersion}，已拒绝降级安装。'
        + #13#10 + '如需继续，请先卸载现有版本（卸载保留用户数据）。',
        mbInformation, MB_OK);
      Result := False;
    end;
end;

; Only packaged program files are tracked for uninstall. User config/output/cache are not deleted.
