// synthetic fixture
package tests

import "testing"

func TestPlaceholder(t *testing.T) {
	if 1 != 1 {
		t.Fatal("arithmetic broke")
	}
}
