; Puck Utility installer for Windows 10 / 11 (x64), Inno Setup 6.2+.
;
; Built by scripts/build-win32-installer.sh, which passes:
;   /DAppVersion=1.3.0
;   /DSourceDir=<PyInstaller one-dir output + data folders>
;   /DOutputDir=<where the setup .exe goes>
;   /DPeakSetupFile=<PeakOemDrv.exe>   optional; without it the PEAK driver
;                                      setup is downloaded at install time
;   /DPeakUrl=<url>                    optional; where that download comes from
;
; Components (all preselected; the PEAK one is deselected when the driver is
; already installed):
;   app      Puck Utility, the GUI and the command line (always)
;   peak     PEAK-System driver setup: PCAN-USB driver + PCAN-Basic API
;            (PCANBasic.dll, which python-can's pcan interface loads)
;   canable  CANable / CandleLight. The adapter needs no driver: its firmware
;            carries Microsoft OS descriptors, so Windows binds its built-in
;            WinUSB driver on first plug-in. Flashing CANable firmware uses
;            STM32CubeProgrammer (which also installs the STM32 DFU driver);
;            ST does not allow redistributing it, so when it is missing the
;            finish page offers ST's download page.
;
; Silent install: setup.exe /VERYSILENT [/COMPONENTS="app,peak,canable"]
; (the PEAK setup then runs silently too: /exenoui /qn /norestart).

#ifndef AppVersion
  #error AppVersion is not defined (pass /DAppVersion=x.y.z)
#endif
#ifndef SourceDir
  #error SourceDir is not defined (pass /DSourceDir=...)
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
#ifdef PeakSetupFile
  #if !FileExists(PeakSetupFile)
    #error PeakSetupFile does not exist
  #endif
  #define PeakBundled "True"
#else
  #define PeakBundled "False"
#endif

#define AppName      "Puck Utility"
#define AppExe       "PuckUtilityApp.exe"
#define AppPublisher "Barrett Technology"
#define PeakSetup    "PeakOemDrv.exe"
#ifndef PeakUrl
  #define PeakUrl    "https://web.barrett.com/support/Puck_ControlLibrary/PeakOemDrv.exe"
#endif
#define CubeProgUrl  "https://www.st.com/en/development-tools/stm32cubeprog.html"

[Setup]
AppId={{ACC7EF4F-F50B-48A3-ACCA-7B4D1D40E2DD}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://barrett.com
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppPublisher}\{#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=PuckUtilityApp-win-v{#AppVersion}-setup
SetupIconFile={#SourceDir}\puckutility\data\images\BarrettIcon.ico
UninstallDisplayIcon={app}\{#AppExe}
; 64-bit Python build; Windows 10 is NT 10.0 (Windows 11 too).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Drivers and Program Files need administrator rights.
PrivilegesRequired=admin
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ChangesEnvironment=yes
CloseApplications=yes
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif

[Types]
Name: "full";   Description: "Puck Utility with Peak and CANable adapter support"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app";     Description: "Puck Utility {#AppVersion}"; Types: full custom; Flags: fixed
Name: "peak";    Description: "Peak PCAN-USB driver and PCAN-Basic API (PEAK-System setup)"; Types: full
Name: "canable"; Description: "CANable / CandleLight (built-in WinUSB driver; firmware flashing via STM32CubeProgrammer)"; Types: full

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"
Name: "addtopath";   Description: "Add the command-line tool (PuckUtilityAppCLI) to the system &PATH"; Flags: unchecked

[Dirs]
; The working folders a technician fills live beside the executable (see
; puckutility/paths.py APP_DIR); let non-admin users write them.
Name: "{app}\firmware"; Permissions: users-modify
Name: "{app}\config";   Permissions: users-modify
Name: "{app}\logs";     Permissions: users-modify

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "\system-config.ini,\firmware\*,\config\*"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; Components: app
; Technician-edited files: keep the user's copy on upgrade.
Source: "{#SourceDir}\system-config.ini"; DestDir: "{app}"; Permissions: users-modify; \
    Flags: onlyifdoesntexist uninsneveruninstall; Components: app
Source: "{#SourceDir}\firmware\*"; DestDir: "{app}\firmware"; \
    Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist; Components: app
Source: "{#SourceDir}\config\*"; DestDir: "{app}\config"; \
    Flags: onlyifdoesntexist recursesubdirs createallsubdirs skipifsourcedoesntexist; Components: app
#ifdef PeakSetupFile
Source: "{#PeakSetupFile}"; DestDir: "{tmp}"; DestName: "{#PeakSetup}"; \
    Flags: deleteafterinstall; Components: peak
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} User Guide"; Filename: "{app}\PuckUtilityAppGuide.pdf"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
    ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
    Tasks: addtopath; Check: NotOnPath(ExpandConstant('{app}'))

[Run]
Filename: "{#CubeProgUrl}"; \
    Description: "Open the STM32CubeProgrammer download page (needed only to flash CANable firmware)"; \
    Flags: postinstall shellexec unchecked skipifsilent; Components: canable; \
    Check: not CubeProgrammerInstalled
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; \
    Flags: postinstall nowait skipifsilent

[Code]
var
  DownloadPage: TDownloadWizardPage;
  ComponentsPageSeen: Boolean;
  PeakNeedsRestart: Boolean;

function PeakInstalled: Boolean;
begin
  // The PEAK setup installs PCAN-Basic into System32 ({sys} in 64-bit mode).
  Result := FileExists(ExpandConstant('{sys}\PCANBasic.dll'));
end;

function CubeProgrammerInstalled: Boolean;
var
  Rel: String;
begin
  // The same locations puckutility/controllers/canable.py searches.
  Rel := '\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe';
  Result := FileExists(ExpandConstant('{commonpf64}') + Rel) or
            FileExists(ExpandConstant('{commonpf32}') + Rel);
end;

function PeakSetupPath: String;
begin
  Result := ExpandConstant('{tmp}\{#PeakSetup}');
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing),
    'Downloading the PEAK PCAN driver setup...', nil);
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Version: String;
begin
  // First visit only, so the user's own choice is never overridden.
  if (CurPageID = wpSelectComponents) and not ComponentsPageSeen then
  begin
    ComponentsPageSeen := True;
    if PeakInstalled then
    begin
      WizardSelectComponents('!peak');
      if not GetVersionNumbersString(ExpandConstant('{sys}\PCANBasic.dll'), Version) then
        Version := '';
      // Items: 0 app, 1 peak, 2 canable
      WizardForm.ComponentsList.ItemCaption[1] :=
        WizardForm.ComponentsList.ItemCaption[1] + ' - already installed ' + Version;
    end;
    if not CubeProgrammerInstalled then
      WizardForm.ComponentsList.ItemCaption[2] :=
        WizardForm.ComponentsList.ItemCaption[2] + ' - STM32CubeProgrammer not found';
  end;
end;

// Fetch the PEAK setup when this installer does not carry it.
function DownloadPeak(Interactive: Boolean): Boolean;
begin
  Result := True;
  if {#PeakBundled} or not WizardIsComponentSelected('peak') or FileExists(PeakSetupPath) then
    Exit;
  if Interactive then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('{#PeakUrl}', '{#PeakSetup}', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        if not DownloadPage.AbortedByUser then
          SuppressibleMsgBox('Could not download the PEAK driver setup:' + #13#10 +
            GetExceptionMessage + #13#10#13#10 +
            'Puck Utility will be installed without it. Install the PEAK driver ' +
            'later from https://www.peak-system.com.', mbError, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end
  else
    try
      DownloadTemporaryFile('{#PeakUrl}', '{#PeakSetup}', '', nil);
    except
      Log('PEAK setup download failed: ' + GetExceptionMessage);
    end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
    Result := DownloadPeak(True);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Silent installs never press Next on the Ready page.
  DownloadPeak(False);
  Result := '';
end;

procedure RunPeakSetup;
var
  Params: String;
  Code: Integer;
begin
  if not FileExists(PeakSetupPath) then
  begin
    Log('PEAK setup not available; skipped');
    Exit;
  end;
  // Interactive: PEAK's own wizard (its licence, its feature choices).
  // Silent: no UI, no reboot; a needed reboot is reported below.
  if WizardSilent then
    Params := '/exenoui /qn /norestart'
  else
    Params := '';
  WizardForm.StatusLabel.Caption := 'Installing the PEAK PCAN driver...';
  if Exec(PeakSetupPath, Params, '', SW_SHOW, ewWaitUntilTerminated, Code) then
  begin
    Log(Format('PEAK setup exit code %d', [Code]));
    // 3010 / 1641: success, restart required; 1602: cancelled by the user.
    if (Code = 3010) or (Code = 1641) then
      PeakNeedsRestart := True
    else if (Code <> 0) and (Code <> 1602) then
      SuppressibleMsgBox(Format('The PEAK driver setup ended with code %d. ' +
        'Peak PCAN-USB adapters may not work until the driver is installed ' +
        'from https://www.peak-system.com.', [Code]), mbError, MB_OK, IDOK);
  end
  else
    SuppressibleMsgBox('Could not start the PEAK driver setup: ' +
      SysErrorMessage(Code), mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsComponentSelected('peak') then
    RunPeakSetup;
end;

function NeedRestart: Boolean;
begin
  Result := PeakNeedsRestart;
end;

// --- PATH ------------------------------------------------------------------
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function NotOnPath(Dir: String): Boolean;
var
  Path: String;
begin
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Path) then
  begin
    Result := True;
    Exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

procedure RemoveFromPath(Dir: String);
var
  Path: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Path) then
    Exit;
  P := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';');
  if P = 0 then
    Exit;
  // P indexes ';' + Path, so the entry starts at P in Path and is followed by
  // its ';' separator, if any; a ';' left at the end (it was last) is dropped.
  Delete(Path, P, Length(Dir) + 1);
  if (Length(Path) > 0) and (Path[Length(Path)] = ';') then
    Delete(Path, Length(Path), 1);
  RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
