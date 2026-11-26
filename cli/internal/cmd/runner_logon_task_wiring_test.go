package cmd

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/preloop/preloop/cli/internal/testenv"
)

// fakeSchtasks records schtasks invocations and answers them from a table
// of task name -> definition XML, so enable, disable and status run on
// every OS without the Task Scheduler.
type fakeSchtasks struct {
	calls [][]string
	tasks map[string]string
	// createErr, when set, is what /Create returns together with createOut.
	createErr error
	createOut string
	// deleteErr, when set, is what /Delete returns for an existing task.
	deleteErr error
	statusCSV string
}

func installFakeSchtasks(t *testing.T, userID, taskName string) *fakeSchtasks {
	t.Helper()
	fake := &fakeSchtasks{tasks: map[string]string{}}
	previousRun := runWindowsSchtasks
	previousIdentity := windowsLogonTaskIdentity
	runWindowsSchtasks = func(args ...string) (string, error) {
		fake.calls = append(fake.calls, args)
		name := ""
		for i, arg := range args {
			if arg == "/TN" && i+1 < len(args) {
				name = args[i+1]
			}
		}
		switch args[0] {
		case "/Create":
			if fake.createErr != nil {
				return fake.createOut, fake.createErr
			}
			for i, arg := range args {
				if arg == "/XML" && i+1 < len(args) {
					raw, err := os.ReadFile(args[i+1])
					if err != nil {
						return "cannot read xml", err
					}
					fake.tasks[name] = decodeWindowsTaskOutput(raw)
				}
			}
			return "SUCCESS: The scheduled task \"" + name + "\" has successfully been created.", nil
		case "/Delete":
			if _, ok := fake.tasks[name]; !ok {
				return "ERROR: The system cannot find the file specified.", errors.New("exit status 1")
			}
			if fake.deleteErr != nil {
				return "ERROR: Access is denied.", fake.deleteErr
			}
			delete(fake.tasks, name)
			return "SUCCESS", nil
		case "/Query":
			definition, ok := fake.tasks[name]
			if !ok {
				return "ERROR: The system cannot find the file specified.", errors.New("exit status 1")
			}
			if contains(args, "/XML") {
				return definition, nil
			}
			return fake.statusCSV, nil
		case "/Run", "/End":
			if _, ok := fake.tasks[name]; !ok {
				return "ERROR: The system cannot find the file specified.", errors.New("exit status 1")
			}
			return "SUCCESS", nil
		}
		return "", errors.New("unexpected schtasks verb " + args[0])
	}
	windowsLogonTaskIdentity = func() (string, string) { return userID, taskName }
	t.Cleanup(func() {
		runWindowsSchtasks = previousRun
		windowsLogonTaskIdentity = previousIdentity
	})
	return fake
}

func (f *fakeSchtasks) verbs() []string {
	out := make([]string, 0, len(f.calls))
	for _, call := range f.calls {
		out = append(out, call[0])
	}
	return out
}

func TestWriteWindowsScheduledTaskRegistersPerUserTaskWithoutElevation(t *testing.T) {
	home := testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `DESKTOP-1\jane`, "PreloopRunner-jane")
	// A 0.17.0 task under the shared name, owned by this user.
	fake.tasks[windowsLegacyLogonTaskName] = `<Task><Principals><Principal><UserId>DESKTOP-1\jane</UserId></Principal></Principals></Task>`

	var out bytes.Buffer
	if err := writeWindowsScheduledTask(`C:\Users\jane\AppData\Local\Preloop\bin\preloop.exe`, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Installed logon task PreloopRunner-jane for DESKTOP-1\\jane") ||
		!strings.Contains(out.String(), "without admin rights") {
		t.Fatalf("output = %q", out.String())
	}
	definition, ok := fake.tasks["PreloopRunner-jane"]
	if !ok {
		t.Fatalf("task not registered; calls = %v", fake.calls)
	}
	def, err := parseWindowsTaskXML([]byte(definition))
	if err != nil {
		t.Fatal(err)
	}
	if def.UserID != `DESKTOP-1\jane` || def.LogonType != "InteractiveToken" || def.RunLevel != "LeastPrivilege" ||
		def.ExecutionTimeLimit != windowsTaskNoTimeLimit || !def.TriggerScopedToUser {
		t.Fatalf("registered definition = %+v", def)
	}
	if _, stillThere := fake.tasks[windowsLegacyLogonTaskName]; stillThere {
		t.Fatal("the user's 0.17.0 shared-name task must be removed so only one runner starts at logon")
	}
	xmlPath := filepath.Join(home, ".preloop", "runner-task.xml")
	raw, err := os.ReadFile(xmlPath)
	if err != nil {
		t.Fatal(err)
	}
	if raw[0] != 0xFF || raw[1] != 0xFE {
		t.Fatal("the definition file must be UTF-16LE with a BOM, as declared")
	}
	script, err := os.ReadFile(filepath.Join(home, ".preloop", "runner-task.ps1"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(script), `preloop.exe' runner fg`) {
		t.Fatalf("launcher = %q", script)
	}
	if !strings.Contains(definition, "-File \""+filepath.Join(home, ".preloop", "runner-task.ps1")+"\"") {
		t.Fatalf("task must run the launcher script:\n%s", definition)
	}
	// Create first, then the legacy task is inspected before it is deleted;
	// no elevation probe, no /SC ONLOGON.
	if got := strings.Join(fake.verbs(), ","); got != "/Create,/Query,/Delete" {
		t.Fatalf("schtasks verbs = %s", got)
	}
	for _, arg := range fake.calls[0] {
		if arg == "/SC" || arg == "/RL" || arg == "/RU" {
			t.Fatalf("/Create must register from XML only, got %v", fake.calls[0])
		}
	}
}

func TestWriteWindowsScheduledTaskPolicyErrorIsActionable(t *testing.T) {
	testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `CORP\jane`, "PreloopRunner-jane")
	fake.createErr = errors.New("exit status 1")
	fake.createOut = "ERROR: Access is denied."
	previousPolicy := windowsTaskCreationPolicy
	windowsTaskCreationPolicy = func() windowsTaskPolicy {
		return windowsTaskPolicy{Checked: true, CreationProhibited: true}
	}
	t.Cleanup(func() { windowsTaskCreationPolicy = previousPolicy })

	var out bytes.Buffer
	err := writeWindowsScheduledTask(`C:\preloop.exe`, &out)
	if err == nil {
		t.Fatal("want the policy error")
	}
	for _, want := range []string{
		"task_creation_prohibited_by_policy",
		windowsTaskPolicyName,
		"preloop runner fg",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("missing %q in %v", want, err)
		}
	}
	if strings.Contains(err.Error(), "elevated") {
		t.Fatalf("no elevation advice for a per-user task: %v", err)
	}
	// The setup flow prints that error and the manual command instead of
	// failing the login.
	previousInstall := setupInstallRunner
	setupInstallRunner = func(w io.Writer) error { return writeWindowsScheduledTask(`C:\preloop.exe`, w) }
	t.Cleanup(func() { setupInstallRunner = previousInstall })
	out.Reset()
	tty := true
	if err := performRunnerSetup(runnerSetupRequest{
		Out:       &out,
		In:        strings.NewReader("y\n\n"),
		Policy:    &runnerPolicy{Requirement: "optional"},
		TTY:       &tty,
		Installed: func() bool { return false },
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), windowsTaskPolicyName) || !strings.Contains(out.String(), runnerSetupManualCommand) {
		t.Fatalf("setup output = %q", out.String())
	}
}

func TestFindWindowsRunnerTaskIgnoresAnotherUsersLegacyTask(t *testing.T) {
	testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `DESKTOP-1\bob`, "PreloopRunner-bob")
	fake.tasks[windowsLegacyLogonTaskName] = `<Task><Principals><Principal><UserId>DESKTOP-1\jane</UserId><LogonType>InteractiveToken</LogonType></Principal></Principals></Task>`

	if name, _, ok := findWindowsRunnerTask(); ok {
		t.Fatalf("jane's task must not count as bob's runner (found %s)", name)
	}
	state := windowsRunnerServiceState()
	if !strings.HasPrefix(state, "not installed (logon task PreloopRunner-bob") {
		t.Fatalf("state = %q", state)
	}
	// Bob's own per-user task wins once it exists.
	fake.tasks["PreloopRunner-bob"] = windowsLogonTaskXML(windowsLogonTaskSpec{
		Name: "PreloopRunner-bob", UserID: `DESKTOP-1\bob`, PowerShell: "p.exe", ScriptPath: "s.ps1", WorkingDir: "w",
	})
	fake.statusCSV = "\"\\PreloopRunner-bob\",\"N/A\",\"Running\"\r\n"
	name, def, ok := findWindowsRunnerTask()
	if !ok || name != "PreloopRunner-bob" || def.UserID != `DESKTOP-1\bob` {
		t.Fatalf("found %s %+v %v", name, def, ok)
	}
	state = windowsRunnerServiceState()
	for _, want := range []string{
		"task PreloopRunner-bob (Running)",
		"context: logon task PreloopRunner-bob runs as DESKTOP-1\\bob in the interactive session, least privilege (no admin rights)",
		"lock: keeps running while the screen is locked",
		"sign-out: ends with the session",
	} {
		if !strings.Contains(state, want) {
			t.Errorf("state missing %q:\n%s", want, state)
		}
	}
}

func TestFindWindowsRunnerTaskUpgradesOwnLegacyTask(t *testing.T) {
	testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `DESKTOP-1\jane`, "PreloopRunner-jane")
	fake.tasks[windowsLegacyLogonTaskName] = `<Task><Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers><Principals><Principal><UserId>desktop-1\JANE</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals><Settings><ExecutionTimeLimit>PT72H</ExecutionTimeLimit></Settings></Task>`
	name, _, ok := findWindowsRunnerTask()
	if !ok || name != windowsLegacyLogonTaskName {
		t.Fatalf("own legacy task must be found: %s %v", name, ok)
	}
	state := windowsRunnerServiceState()
	if !strings.Contains(state, "upgrade: the task stops the runner after PT72H and starts for any user; re-run preloop runner enable") {
		t.Fatalf("state = %q", state)
	}
	// Disable removes the legacy task and the launcher files.
	home, _ := os.UserHomeDir()
	if err := os.MkdirAll(filepath.Join(home, ".preloop"), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, file := range []string{"runner-task.ps1", "runner-task.xml"} {
		if err := os.WriteFile(filepath.Join(home, ".preloop", file), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := removeWindowsScheduledTask(); err != nil {
		t.Fatal(err)
	}
	if _, stillThere := fake.tasks[windowsLegacyLogonTaskName]; stillThere {
		t.Fatal("legacy task not deleted")
	}
	for _, file := range []string{"runner-task.ps1", "runner-task.xml"} {
		if _, err := os.Stat(filepath.Join(home, ".preloop", file)); !os.IsNotExist(err) {
			t.Fatalf("%s must be removed: %v", file, err)
		}
	}
	// A second disable with nothing installed returns the schtasks error,
	// which runRunnerDisable already tolerates for --delete.
	if err := removeWindowsScheduledTask(); err == nil {
		t.Fatal("removing a missing task must report it")
	}
}

func TestWriteWindowsScheduledTaskLeavesAnotherUsersLegacyTask(t *testing.T) {
	testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `DESKTOP-1\bob`, "PreloopRunner-bob")
	// An administrator account could delete jane's task; enable must not.
	fake.tasks[windowsLegacyLogonTaskName] = `<Task><Principals><Principal><UserId>DESKTOP-1\jane</UserId></Principal></Principals></Task>`
	var out bytes.Buffer
	if err := writeWindowsScheduledTask(`C:\preloop.exe`, &out); err != nil {
		t.Fatal(err)
	}
	if _, kept := fake.tasks[windowsLegacyLogonTaskName]; !kept {
		t.Fatal("another user's 0.17.0 task must survive this user's enable")
	}
	if _, ok := fake.tasks["PreloopRunner-bob"]; !ok {
		t.Fatal("bob's task not registered")
	}
	if got := strings.Join(fake.verbs(), ","); got != "/Create,/Query" {
		t.Fatalf("schtasks verbs = %s", got)
	}
	if strings.Contains(out.String(), "Warning") {
		t.Fatalf("nothing to warn about: %q", out.String())
	}
}

func TestWriteWindowsScheduledTaskReportsAFailedLegacyDelete(t *testing.T) {
	testenv.SetTempHome(t)
	fake := installFakeSchtasks(t, `DESKTOP-1\jane`, "PreloopRunner-jane")
	fake.tasks[windowsLegacyLogonTaskName] = `<Task><Principals><Principal><UserId>desktop-1\Jane</UserId></Principal></Principals></Task>`
	fake.deleteErr = errors.New("exit status 1")
	var out bytes.Buffer
	if err := writeWindowsScheduledTask(`C:\preloop.exe`, &out); err != nil {
		t.Fatalf("the per-user task is installed; a legacy cleanup failure is a warning, got %v", err)
	}
	if !strings.Contains(out.String(), "Installed logon task PreloopRunner-jane") {
		t.Fatalf("output = %q", out.String())
	}
	if !strings.Contains(out.String(), "Warning: the task PreloopRunner registered by an earlier CLI for this user could not be removed (ERROR: Access is denied.); two runners would start at your next logon. Remove it with: schtasks /Delete /TN PreloopRunner /F") {
		t.Fatalf("output = %q", out.String())
	}
}
