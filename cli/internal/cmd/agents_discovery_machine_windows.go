package cmd

import "golang.org/x/sys/windows/registry"

func platformMachineID() string {
	// Native registry access avoids running a PATH-selected process as SYSTEM.
	key, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Cryptography`, registry.QUERY_VALUE|registry.WOW64_64KEY)
	if err != nil {
		return ""
	}
	defer key.Close()
	id, _, err := key.GetStringValue("MachineGuid")
	if err != nil {
		return ""
	}
	return id
}
