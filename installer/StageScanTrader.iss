; 階段掃描下單 安裝程式（Inno Setup 6）
; 由 .github/workflows/build-installer.yml 自動打包；版本由 git 標籤（例如 v1.0.0）帶入。
; 安裝在使用者自己的 AppData（不需要系統管理員權限）；使用者資料在 %LOCALAPPDATA%\StageScanTrader，更新與移除都會保留。

#define AppName "階段掃描下單"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8E5C2B7A-4C61-4C2A-9F0E-5A1D3B7C9E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=stage-scan
AppPublisherURL=https://github.com/royaleo1207/stage-scan
AppSupportURL=https://github.com/royaleo1207/stage-scan/releases
DefaultDirName={localappdata}\Programs\StageScanTrader
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=StageScanTrader-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\app\installer\icon.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0

[Languages]
Name: "cht"; MessagesFile: "ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; GroupDescription: "捷徑："

[InstallDelete]
; 更新時先清掉舊版程式與套件，避免殘留（使用者資料不在這裡，不受影響）
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Files]
Source: "..\build\StageScanTrader\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\StageScanTrader.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\installer\icon.ico"; Comment: "選股、凱基 SuperPy 下單、停損停利監控"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\StageScanTrader.pyw"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\installer\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\StageScanTrader.pyw"""; WorkingDir: "{app}\app"; Description: "立即開啟{#AppName}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app"

[Messages]
cht.WelcomeLabel2=這會在你的電腦安裝 [name/ver]。%n%n使用前需要：%n• 凱基證券帳戶，並線上簽署「證券 API 服務風險預告書暨使用同意書」%n• 依凱基說明安裝 CA 憑證%n• 美股另需向營業員申請複委託 API%n%n帳號密碼只在你的電腦上使用，不會上傳。本程式只執行你設定的規則，不保證獲利。
