!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef PAYLOAD_DIR
  !error "PAYLOAD_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef APP_ICON
  !error "APP_ICON is required"
!endif
!ifndef HELPER_EXE
  !error "HELPER_EXE is required"
!endif

Unicode true
RequestExecutionLevel user
CRCCheck on
SetCompressor /SOLID lzma
Name "Codex 路由 ${APP_VERSION}"
OutFile "${OUTPUT_FILE}"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
InstallDir "$LOCALAPPDATA\CodexMultiKey"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CodexMultiKey" "InstallLocation"
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "Codex 路由"
VIAddVersionKey "FileDescription" "Codex 路由安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 Codex Router"

!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "此向导将为当前用户安装 Codex 路由。您可以在下一步选择安装目录。"
!define MUI_DIRECTORYPAGE_TEXT_TOP "请选择 Codex 路由的安装目录。已有安装会安全覆盖，用户数据会迁移到新目录。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\CodexMultiKey.exe"
!define MUI_FINISHPAGE_RUN_TEXT "运行 Codex 路由"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "安装"
  SetShellVarContext current
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD_DIR}\*"
  WriteUninstaller "$PLUGINSDIR\payload\uninstall.exe"
  SetOutPath "$PLUGINSDIR"
  File /oname=installctl.exe "${HELPER_EXE}"
  DetailPrint "正在校验旧安装、迁移数据并更新卸载登记..."
  nsExec::ExecToStack '"$PLUGINSDIR\installctl.exe" install "$PLUGINSDIR\payload" "$INSTDIR" "${APP_VERSION}" "$LOCALAPPDATA\CodexMultiKeyInstaller\install.log"'
  Pop $0
  Pop $1
  StrCmp $0 "0" +3
    MessageBox MB_ICONSTOP|MB_OK "安装失败：$1$\r$\n详情见 $LOCALAPPDATA\CodexMultiKeyInstaller\install.log"
    Abort
  DetailPrint "Codex 路由 ${APP_VERSION} 安装完成。"
SectionEnd

Function un.onInit
  SetShellVarContext current
  ; Keep NSIS's original uninstaller directory in $INSTDIR. installctl checks
  ; that it equals HKCU InstallLocation before touching any files.
FunctionEnd

Section "Uninstall"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=installctl.exe "${HELPER_EXE}"
  DetailPrint "正在停止程序并卸载 Codex 路由..."
  nsExec::ExecToStack '"$PLUGINSDIR\installctl.exe" uninstall "" "$INSTDIR" "${APP_VERSION}" "$LOCALAPPDATA\CodexMultiKeyInstaller\install.log"'
  Pop $0
  Pop $1
  StrCmp $0 "0" +3
    MessageBox MB_ICONSTOP|MB_OK "卸载失败：$1$\r$\n详情见 $LOCALAPPDATA\CodexMultiKeyInstaller\install.log"
    Abort
  IfFileExists "$INSTDIR\.codexmk-uninstalled.json" 0 +2
    DetailPrint "已保留用户修改或添加的文件；下次安装可安全接管此目录。"
  RMDir "$INSTDIR"
  DetailPrint "Codex 路由卸载完成。"
SectionEnd
