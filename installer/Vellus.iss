; Inno Setup script for Vellus. Build with build-installer.ps1.
;
; Vellus installs for the current user and needs no administrator rights.
; WinFsp, which provides the drive letter, is a system component: if it is
; missing, its own signed installer is run and Windows asks for permission.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\Vellus"
#endif
; The WinFsp release bundled with this version of Vellus. It is installed only
; when WinFsp is absent; an existing installation is never replaced.
#define WinFspMsi "winfsp-2.1.25156.msi"

[Setup]
AppId={{6B0E7C0E-6A0B-4F0C-9B56-3B7D2C4E91A7}
AppName=Vellus
AppVersion={#AppVersion}
AppVerName=Vellus {#AppVersion}
DefaultDirName={autopf}\Vellus
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; "x64" rather than "x64compatible" so that Inno Setup releases before 6.3 can build this.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
LicenseFile=..\LICENSE
InfoBeforeFile=before-install.txt
OutputDir=Output
OutputBaseFilename=Vellus-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Vellus
UninstallDisplayIcon={app}\Vellus.exe
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WinFspMsi}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWinFsp

[Icons]
Name: "{autoprograms}\Vellus"; Filename: "{app}\Vellus.exe"
Name: "{autodesktop}\Vellus"; Filename: "{app}\Vellus.exe"; Tasks: desktopicon

[Run]
Filename: "msiexec.exe"; Parameters: "/i ""{tmp}\{#WinFspMsi}"" /passive /norestart"; \
    StatusMsg: "Installing WinFsp, which provides the drive letter..."; \
    Check: NeedsWinFsp; Flags: waituntilterminated
Filename: "{app}\Vellus.exe"; Description: "Start Vellus"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsWinFsp: Boolean;
var
  InstallDir: String;
begin
  Result := not RegQueryStringValue(HKLM32, 'SOFTWARE\WinFsp', 'InstallDir', InstallDir);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and NeedsWinFsp then
    SuppressibleMsgBox(
      'WinFsp was not installed, so Vellus cannot provide a drive letter yet.' + #13#10 + #13#10 +
      'The Vellus window will still list, open and download your files. To get the ' +
      'drive letter, install WinFsp from https://winfsp.dev and start Vellus again.',
      mbInformation, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExitCode: Integer;
begin
  { Asked before the program files go, because Vellus itself does the removal. }
  if CurUninstallStep = usUninstall then
    if SuppressibleMsgBox(
         'Also remove your Vellus settings, saved sign-in and cached copies of your ' +
         'files from this PC?' + #13#10 + #13#10 + 'Nothing in iCloud is deleted.',
         mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      Exec(ExpandConstant('{app}\Vellus.exe'), '--remove-user-data', '', SW_HIDE,
           ewWaitUntilTerminated, ExitCode);
end;
