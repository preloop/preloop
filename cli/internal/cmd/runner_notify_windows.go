//go:build windows

package cmd

import (
	"os"
	"path/filepath"
)

// hostNoticeScript raises a Windows toast. The text arrives in the
// PRELOOP_NOTICE_TEXT environment variable and is XML-escaped inside the
// script, so the script itself is constant. A runner installed as a service
// in session 0 has no desktop; the toast then fails silently and the runner
// log line remains the notice.
const hostNoticeScript = `$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($env:PRELOOP_NOTICE_TEXT)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>Preloop</text><text>$t</text></binding></visual></toast>")
$n = [Windows.UI.Notifications.ToastNotification]::new($x)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Preloop').Show($n)`

func hostNoticeCommand(text string) (string, []string, []string, bool) {
	root := os.Getenv("SystemRoot")
	if root == "" {
		root = `C:\Windows`
	}
	powershell := filepath.Join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
	env := append(os.Environ(), "PRELOOP_NOTICE_TEXT="+text)
	return powershell, []string{"-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", hostNoticeScript}, env, true
}
