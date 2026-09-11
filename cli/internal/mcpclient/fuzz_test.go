package mcpclient

import (
	"bytes"
	"reflect"
	"testing"
	"testing/iotest"
)

// FuzzMCPEventStream exercises untrusted MCP replies, including progress events,
// malformed JSON, multiline data and truncated frames. Parsing must not panic
// or depend on how the transport splits an identical byte stream into reads.
func FuzzMCPEventStream(f *testing.F) {
	f.Fuzz(func(t *testing.T, raw []byte) {
		if len(raw) > 64*1024 {
			t.Skip()
		}
		var wholeProgress, fragmentedProgress bytes.Buffer
		whole, wholeErr := decodeSSEResponse(bytes.NewReader(raw), &wholeProgress)
		fragmented, fragmentedErr := decodeSSEResponse(iotest.OneByteReader(bytes.NewReader(raw)), &fragmentedProgress)
		if (wholeErr == nil) != (fragmentedErr == nil) {
			t.Fatalf("read boundaries changed acceptance: whole=%v fragmented=%v", wholeErr, fragmentedErr)
		}
		if !reflect.DeepEqual(whole, fragmented) || wholeProgress.String() != fragmentedProgress.String() {
			t.Fatal("read boundaries changed response or progress")
		}
		if wholeErr == nil && whole == nil {
			t.Fatal("successful stream returned no response")
		}
	})
}
