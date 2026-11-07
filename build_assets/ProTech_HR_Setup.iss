; يُشغَّل هذا الملف عبر Inno Setup Compiler (ISCC.exe) على Windows فقط،
; بعد إنتاج مجلد dist\ProTech_HR_Portable عبر ProTech_HR.spec أعلاه.
; النتيجة: ProTech_HR_Setup_v23.0.1.exe

#define AppVersion "23.0.1"
#define AppName "نظام برو تك لإدارة الموارد البشرية"
#define AppPublisher "شركة برو تك"
#define AppExeName "ProTech_HR.exe"
#ifndef SourceDir
  #define SourceDir "..\dist\ProTech_HR_Portable"
#endif

[Setup]
AppId={{B6B6E6C1-17A9-4E9C-9C77-PROTECHHR9}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoCompany=ProTech
VersionInfoDescription=ProTech HR Setup (Stable)
VersionInfoProductName=ProTech HR
VersionInfoProductVersion=23.0.1.0
VersionInfoVersion=23.0.1.0
VersionInfoCopyright=Copyright (C) 2026 Abu Omar / ProTech - MIT
DefaultDirName={autopf}\ProTech\ProTech HR
DefaultGroupName=ProTech HR
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=ProTech_HR_Setup_v{#AppVersion}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
UsePreviousAppDir=no
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
Uninstallable=yes
AppMutex=Local\ProTechHR-v18-application
MinVersion=10.0.17763
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\assets\protech_hr.ico
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\LICENSE
#ifdef PROTECH_SIGNED_BUILD
SignTool=ProTechSigner
SignedUninstaller=yes
#endif
; بدون شهادة توقيع تجارية — ستظهر رسالة SmartScreen عند أول تشغيل، هذا متوقع.

[Languages]
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"

[Files]
; لا تُضمَّن بيانات التشغيل المحمولة في Setup مطلقًا: التثبيت الجديد ينشئ
; قاعدة نظيفة في ProgramData، والتحديث لا يكتب فوق قاعدة مستخدم قائمة.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "portable_mode.flag,ProTech_HR_Data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\ProTech\HR\Data"; Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "إنشاء أيقونة على سطح المكتب"; GroupDescription: "أيقونات إضافية:"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "تشغيل البرنامج الآن"; Flags: nowait postinstall skipifsilent

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B6B6E6C1-17A9-4E9C-9C77-PROTECHHR9}_is1';
  ModeNewInstall = 0;
  ModeRepair = 1;
  ModeUpdate = 2;
  ModeRemove = 3;

var
  PreviousInstallFound: Boolean;
  PreviousDataFound: Boolean;
  PreviousVersion: String;
  PreviousUninstaller: String;
  InstallModePage: TInputOptionWizardPage;

function RegisteredProgramPath(RootKey: Integer): String;
var
  InstallLocation: String;
begin
  Result := '';
  if RegQueryStringValue(RootKey, UninstallKey, 'InstallLocation', InstallLocation) then
  begin
    InstallLocation := RemoveQuotes(InstallLocation);
    if FileExists(AddBackslash(InstallLocation) + '{#AppExeName}') then
      Result := AddBackslash(InstallLocation) + '{#AppExeName}';
  end;
end;

function DetectPreviousInstall(): Boolean;
begin
  { وجود مجلد البيانات وحده لا يعني أن البرنامج ما زال مثبتًا. }
  Result :=
    (RegisteredProgramPath(HKCU) <> '') or
    (RegisteredProgramPath(HKLM) <> '') or
    FileExists(ExpandConstant('{localappdata}\Programs\ProTechHR\{#AppExeName}')) or
    FileExists(ExpandConstant('{localappdata}\Programs\ProTech HR\{#AppExeName}')) or
    FileExists(ExpandConstant('{autopf}\ProTech\ProTech HR\{#AppExeName}')) or
    FileExists(ExpandConstant('{autopf}\PRO TECH\ProTech HR\ProTech_HR_System.exe')) or
    FileExists(ExpandConstant('{autopf}\PRO TECH\ProTech HR\ProTech_HR.exe'));
end;

function DetectPreviousData(): Boolean;
begin
  Result :=
    DirExists(ExpandConstant('{localappdata}\HRSystemPortable')) or
    DirExists(ExpandConstant('{localappdata}\Programs\ProTechHR\ProTech_HR_Data')) or
    DirExists(ExpandConstant('{localappdata}\Programs\ProTech HR\ProTech_HR_Data')) or
    DirExists(ExpandConstant('{commonappdata}\ProTech\HR\Data')) or
    DirExists(ExpandConstant('{autopf}\PRO TECH\ProTech HR\ProTech_HR_Data'));
end;

function ReadPreviousVersion(): String;
begin
  Result := '';
  if not RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Result) then
    RegQueryStringValue(HKLM, UninstallKey, 'DisplayVersion', Result);
end;

function VersionPart(var Version: String): Integer;
var
  Separator: Integer;
  Part: String;
begin
  Separator := Pos('.', Version);
  if Separator = 0 then
  begin
    Part := Version;
    Version := '';
  end
  else
  begin
    Part := Copy(Version, 1, Separator - 1);
    Delete(Version, 1, Separator);
  end;
  Result := StrToIntDef(Part, 0);
end;

function CompareVersions(LeftVersion, RightVersion: String): Integer;
var
  Index: Integer;
  LeftPart, RightPart: Integer;
begin
  Result := 0;
  for Index := 1 to 4 do
  begin
    LeftPart := VersionPart(LeftVersion);
    RightPart := VersionPart(RightVersion);
    if LeftPart > RightPart then
    begin
      Result := 1;
      Exit;
    end;
    if LeftPart < RightPart then
    begin
      Result := -1;
      Exit;
    end;
  end;
end;

function InitializeSetup(): Boolean;
var
  InstalledVersion: String;
begin
  InstalledVersion := ReadPreviousVersion();
  Result := True;
  if (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersion}') > 0) then
  begin
    MsgBox(
      'يوجد إصدار أحدث مثبت بالفعل (' + InstalledVersion + '). أُلغي التثبيت لحماية البرنامج والبيانات من الرجوع إلى إصدار أقدم.',
      mbError,
      MB_OK
    );
    Result := False;
  end;
end;

function UninstallerFromRegistry(RootKey: Integer): String;
var
  InstallLocation: String;
begin
  Result := '';
  if RegQueryStringValue(RootKey, UninstallKey, 'InstallLocation', InstallLocation) then
  begin
    InstallLocation := RemoveQuotes(InstallLocation);
    if FileExists(AddBackslash(InstallLocation) + 'unins000.exe') then
      Result := AddBackslash(InstallLocation) + 'unins000.exe';
  end;
end;

function FindPreviousUninstaller(): String;
begin
  Result := UninstallerFromRegistry(HKCU);
  if Result <> '' then Exit;
  Result := UninstallerFromRegistry(HKLM);
  if Result <> '' then Exit;

  Result := ExpandConstant('{localappdata}\Programs\ProTechHR\unins000.exe');
  if FileExists(Result) then Exit;
  Result := ExpandConstant('{localappdata}\Programs\ProTech HR\unins000.exe');
  if FileExists(Result) then Exit;
  Result := ExpandConstant('{autopf}\PRO TECH\ProTech HR\unins000.exe');
  if FileExists(Result) then Exit;
  Result := '';
end;

procedure SetInstallModeAvailability();
begin
  PreviousInstallFound := DetectPreviousInstall();
  PreviousDataFound := DetectPreviousData();
  PreviousVersion := ReadPreviousVersion();
  PreviousUninstaller := FindPreviousUninstaller();

  InstallModePage.CheckListBox.ItemEnabled[ModeNewInstall] := not PreviousInstallFound;
  InstallModePage.CheckListBox.ItemEnabled[ModeRepair] := PreviousInstallFound;
  InstallModePage.CheckListBox.ItemEnabled[ModeUpdate] := PreviousInstallFound;
  InstallModePage.CheckListBox.ItemEnabled[ModeRemove] :=
    PreviousInstallFound and (PreviousUninstaller <> '');

  if PreviousInstallFound then
  begin
    if PreviousVersion = '{#AppVersion}' then
      InstallModePage.SelectedValueIndex := ModeRepair
    else
      InstallModePage.SelectedValueIndex := ModeUpdate;
  end
  else
    InstallModePage.SelectedValueIndex := ModeNewInstall;
end;

procedure UpdateNextButtonCaption();
begin
  case InstallModePage.SelectedValueIndex of
    ModeNewInstall: WizardForm.NextButton.Caption := 'تثبيت جديد';
    ModeRepair: WizardForm.NextButton.Caption := 'إصلاح';
    ModeUpdate: WizardForm.NextButton.Caption := 'تحديث';
    ModeRemove: WizardForm.NextButton.Caption := 'إزالة';
  end;
end;

procedure InstallModeChanged(Sender: TObject);
begin
  if WizardForm.CurPageID = InstallModePage.ID then
    UpdateNextButtonCaption();
end;

procedure InitializeWizard();
var
  StatusText: String;
begin
  PreviousInstallFound := DetectPreviousInstall();
  PreviousDataFound := DetectPreviousData();

  if PreviousInstallFound then
    StatusText := 'توجد نسخة فعلية من البرنامج. اختر إصلاحها أو تحديثها أو إزالتها.'
  else if PreviousDataFound then
    StatusText := 'لا توجد نسخة مثبتة، لكن توجد بيانات محفوظة. سيحافظ التثبيت الجديد عليها.'
  else
    StatusText := 'لا توجد نسخة سابقة. التثبيت الجديد جاهز.';

  InstallModePage := CreateInputOptionPage(
    wpWelcome,
    'إدارة تثبيت ProTech HR',
    'اختر العملية المناسبة',
    StatusText,
    True,
    False
  );
  InstallModePage.Add('تثبيت جديد — متاح عند عدم وجود نسخة مثبتة');
  InstallModePage.Add('إصلاح النسخة الحالية — إعادة تثبيت ملفات البرنامج مع الحفاظ على البيانات');
  InstallModePage.Add('تحديث النسخة الحالية — تثبيت الإصدار الجديد مع الحفاظ على البيانات');
  InstallModePage.Add('إزالة النسخة السابقة — تشغيل أداة الإزالة ثم إتاحة تثبيت جديد');
  InstallModePage.CheckListBox.OnClickCheck := @InstallModeChanged;
  SetInstallModeAvailability();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if CurPageID <> InstallModePage.ID then Exit;

  if InstallModePage.SelectedValueIndex = ModeRemove then
  begin
    Result := False;
    if PreviousUninstaller = '' then
    begin
      MsgBox(
        'تعذر العثور على أداة إزالة النسخة السابقة. استخدم «التطبيقات المثبتة» في Windows ثم أعد تشغيل المثبّت.',
        mbError,
        MB_OK
      );
      Exit;
    end;

    if MsgBox(
      'سيُزال البرنامج السابق فقط، وستبقى قاعدة البيانات والنسخ الاحتياطية والمرفقات محفوظة. هل تريد المتابعة؟',
      mbConfirmation,
      MB_YESNO
    ) <> IDYES then Exit;

    if not Exec(
      PreviousUninstaller,
      '/SILENT /SUPPRESSMSGBOXES /NORESTART',
      '',
      SW_SHOWNORMAL,
      ewWaitUntilTerminated,
      ResultCode
    ) then
    begin
      MsgBox('تعذر تشغيل أداة الإزالة.', mbError, MB_OK);
      Exit;
    end;

    if (ResultCode <> 0) or DetectPreviousInstall() then
    begin
      MsgBox(
        'لم تكتمل الإزالة. أغلق البرنامج إن كان مفتوحًا، ثم حاول مرة أخرى أو استخدم إعدادات Windows.',
        mbError,
        MB_OK
      );
      Exit;
    end;

    SetInstallModeAvailability();
    UpdateNextButtonCaption();
    MsgBox(
      'تمت إزالة النسخة السابقة مع الحفاظ على البيانات. يمكنك الآن اختيار «تثبيت جديد».',
      mbInformation,
      MB_OK
    );
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = InstallModePage.ID then
    UpdateNextButtonCaption()
  else if CurPageID = wpReady then
  begin
    case InstallModePage.SelectedValueIndex of
      ModeRepair: WizardForm.NextButton.Caption := 'بدء الإصلاح';
      ModeUpdate: WizardForm.NextButton.Caption := 'بدء التحديث';
    else
      WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
    end;
  end;
end;
