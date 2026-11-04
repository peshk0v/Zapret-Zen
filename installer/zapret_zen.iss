; Zapret-Zen Inno Setup script
; Build via scripts/build_inno_installer.ps1:
;   & ISCC.exe /DAppVersion=2.5.1 /DX64Src=... /DArm64Src=... installer\zapret_zen.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#ifndef VersionInfoVer
  #define VersionInfoVer AppVersion
#endif

#ifndef MyAppName
  #define MyAppName "Zapret-Zen"
#endif

#ifndef MyAppPublisher
  #define MyAppPublisher "peshk0v"
#endif

#ifndef MyAppExeName
  #define MyAppExeName "zapret_zen.exe"
#endif

[Setup]
AppId={{F5A2C73E-9B11-4E6B-8C2D-1A7E5D0B3F91}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={code:GetInstallPath}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible arm64
ArchitecturesInstallIn64BitMode=x64compatible arm64
OutputDir=dist_installer
OutputBaseFilename=install_zapretzen_{#AppVersion}_universal
SetupLogging=yes
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
VersionInfoVersion={#VersionInfoVer}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#VersionInfoVer}
#ifndef SignToolCmd
#else
SignedUninstaller=yes
SignTool={#SignToolCmd} $f
#endif
#ifdef SetupIcon
SetupIconFile={#SetupIcon}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
; English
english.UpdatePageTitle=Update Zapret-Zen
english.UpdatePageDescription=An existing installation was found. The installer will update the program in place.
english.UpdateBody=Installed version: %1%nInstall path: %2%n%nInstalling version: %3%nYour settings, domain/IP lists, installed mods and custom themes will be preserved.
; Русский
russian.UpdatePageTitle=Обновление Zapret-Zen
russian.UpdatePageDescription=Обнаружена существующая установка. Установщик обновит программу.
russian.UpdateBody=Установленная версия: %1%nПуть установки: %2%n%nБудет установлена версия: %3%nВаши настройки, списки доменов/IP, установленные моды и пользовательские темы будут сохранены.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Preserve existing user domain/IP lists when updating (do not overwrite)
#ifdef X64Src
  #if DirExists(X64Src + "\configs")
Source: "{#X64Src}\configs\*"; DestDir: "{app}\configs"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsX64
  #endif
#endif
#ifdef Arm64Src
  #if DirExists(Arm64Src + "\configs")
Source: "{#Arm64Src}\configs\*"; DestDir: "{app}\configs"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsArm64
  #endif
#endif
; Preserve installed mods when updating (do not overwrite)
#ifdef X64Src
  #if DirExists(X64Src + "\mods")
Source: "{#X64Src}\mods\*"; DestDir: "{app}\mods"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsX64
  #endif
#endif
#ifdef Arm64Src
  #if DirExists(Arm64Src + "\mods")
Source: "{#Arm64Src}\mods\*"; DestDir: "{app}\mods"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsArm64
  #endif
#endif
; Bundled Cloudflare WARP binaries (runtime/warp): install only when missing,
; never overwrite a copy the user may have auto-updated to a newer version
#ifdef X64Src
  #if DirExists(X64Src + "\runtime\warp")
Source: "{#X64Src}\runtime\warp\*"; DestDir: "{app}\runtime\warp"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsX64
  #endif
#endif
#ifdef Arm64Src
  #if DirExists(Arm64Src + "\runtime\warp")
Source: "{#Arm64Src}\runtime\warp\*"; DestDir: "{app}\runtime\warp"; Flags: ignoreversion recursesubdirs createallsubdirs onlyifdoesntexist; Check: IsArm64
  #endif
#endif
; Everything else is refreshed on update
#ifdef X64Src
Source: "{#X64Src}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "configs\*,mods\*,runtime\warp\*"; Check: IsX64
#endif
#ifdef Arm64Src
Source: "{#Arm64Src}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "configs\*,mods\*,runtime\warp\*"; Check: IsArm64
#endif

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\{#MyAppExeName}"; IconIndex: 0

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  UNINST_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{F5A2C73E-9B11-4E6B-8C2D-1A7E5D0B3F91}_is1';
  LEGACY_UNINSTALL_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\ZapretZen';
  RUN_KEY = 'Software\Microsoft\Windows\CurrentVersion\Run';

var
  UpdatePage: TWizardPage;
  UpdateTextLabel: TNewStaticText;

function GetRegString(Key: Integer; SubKeyName, ValueName: String): String;
var
  Value: String;
begin
  Result := '';
  if RegQueryStringValue(Key, SubKeyName, ValueName, Value) then
    Result := Value;
end;

function GetRegStringAny(SubKeyName, ValueName: String): String;
begin
  Result := GetRegString(HKLM64, SubKeyName, ValueName);
  if Result <> '' then Exit;
  Result := GetRegString(HKLM32, SubKeyName, ValueName);
  if Result <> '' then Exit;
  Result := GetRegString(HKCU64, SubKeyName, ValueName);
  if Result <> '' then Exit;
  Result := GetRegString(HKCU32, SubKeyName, ValueName);
end;

function GetExistingInstallDir(): String;
begin
  Result := GetRegStringAny(UNINST_KEY, 'Inno Setup: App Path');
  if Result <> '' then Exit;
  Result := GetRegStringAny(UNINST_KEY, 'InstallLocation');
  if Result <> '' then Exit;
  Result := GetRegStringAny(LEGACY_UNINSTALL_KEY, 'InstallLocation');
end;

function GetExistingVersion(): String;
begin
  Result := GetRegStringAny(UNINST_KEY, 'DisplayVersion');
  if Result <> '' then Exit;
  Result := GetRegStringAny(LEGACY_UNINSTALL_KEY, 'DisplayVersion');
  if Result <> '' then Exit;
  Result := GetRegStringAny(LEGACY_UNINSTALL_KEY, 'Version');
end;

function IsAppInstalled(): Boolean;
begin
  Result := GetExistingInstallDir() <> '';
end;

function GetInstallPath(Param: String): String;
begin
  Result := GetExistingInstallDir();
  if Result = '' then
    Result := ExpandConstant('{autopf}\Zapret-Zen');
end;

procedure KillAppImages();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /T /IM zapret_zen.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/F /T /IM winws.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/F /T /IM TgWsProxy_windows.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/F /T /IM tg-ws-proxy.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure RemoveAutostart();
var
  ResultCode: Integer;
begin
  Exec('schtasks.exe', '/Delete /F /TN ZapretZen', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  RegDeleteValue(HKCU, RUN_KEY, 'ZapretZen');
  RegDeleteValue(HKCU, RUN_KEY, 'ZapretHub');
  RegDeleteValue(HKCU, RUN_KEY, 'Zapret-Zen');
  RegDeleteValue(HKLM64, RUN_KEY, 'ZapretZen');
  RegDeleteValue(HKLM64, RUN_KEY, 'ZapretHub');
  RegDeleteValue(HKLM64, RUN_KEY, 'Zapret-Zen');
  Exec('sc.exe', 'stop zapret', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('sc.exe', 'delete zapret', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function IsX64(): Boolean;
begin
  Result := not IsARM64();
end;

procedure InitializeWizard();
begin
  if IsAppInstalled() then
  begin
    UpdatePage := CreateCustomPage(wpWelcome, ExpandConstant('{cm:UpdatePageTitle}'), ExpandConstant('{cm:UpdatePageDescription}'));
    UpdateTextLabel := TNewStaticText.Create(UpdatePage);
    UpdateTextLabel.Parent := UpdatePage.Surface;
    UpdateTextLabel.WordWrap := True;
    UpdateTextLabel.Left := ScaleX(12);
    UpdateTextLabel.Top := ScaleY(12);
    UpdateTextLabel.Width := WizardForm.InnerPage.ClientWidth - ScaleX(24);
    UpdateTextLabel.Caption := FmtMessage(ExpandConstant('{cm:UpdateBody}'), [
      GetExistingVersion(), GetExistingInstallDir(), '{#AppVersion}']);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    KillAppImages();
    RemoveAutostart();
  end;
end;

procedure CurUninstallStepChanged(CurStep: TUninstallStep);
begin
  if CurStep = usUninstall then
  begin
    KillAppImages();
    RemoveAutostart();
  end;
  if CurStep = usPostUninstall then
  begin
    RegDeleteKeyIncludingSubkeys(HKLM64, LEGACY_UNINSTALL_KEY);
    DelTree(ExpandConstant('{app}'), True, True, True);
  end;
end;