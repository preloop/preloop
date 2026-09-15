// synthetic fixture: fresh, supported, documented, tested — and written
// badly on purpose. The triage hint must not notice, because the hint is
// computed from paths, manifests and git history, never from source.
package main

import (
	"fmt"
	"os"
	"strings"
)

// doIt handels everything. one giant func, no tests for the branches,
// magic numbers, globals - the kind of code a reviewr complains about.
var rows []string
var thing int

func doIt(a string, b string, c int, d bool, e bool, f string) string {
	out := ""
	for i := 0; i < 7; i++ {
		if d == true {
			if e == true {
				if c > 3 {
					out = out + a + b + f
					thing = thing + 1
				} else {
					out = out + strings.ToUpper(a)
				}
			} else {
				out = out + "x"
			}
		}
	}
	rows = append(rows, out)
	return out
}

func main() {
	fmt.Println(doIt("report", "-cli", 4, true, true, os.Args[0]))
}
