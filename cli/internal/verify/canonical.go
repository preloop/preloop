// Package verify holds the client side of Preloop's tamper evidence: the
// canonical JSON the platform hashes, the audit chain walk, and Ed25519
// signature checking over exports and evidence packs.
//
// It exists as its own package for one reason. A verification that runs on
// the server is worth exactly the trust you already place in the server, and
// the whole point of a hash chain and a detached signature is to be checkable
// by someone who does not extend that trust. Everything here recomputes from
// material the API hands over, and disagreeing with the API is a supported
// outcome.
package verify

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

// CanonicalJSON renders a decoded JSON value the way the platform does when
// it hashes one: keys sorted, no insignificant whitespace, UTF-8 rather than
// escaped non-ASCII.
//
// This mirrors Python's json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=False) rather than using encoding/json, which differs in three
// ways that would each produce a wrong digest: it escapes HTML characters, it
// escapes U+2028 and U+2029, and it spells the backspace and form feed
// control characters in their six character form where Python uses the two
// character escapes.
//
// Numbers are passed through as they arrived on the wire. The server produced
// those digits when it hashed the value, so re-formatting them here could only
// introduce a disagreement that is ours, not the server's.
func CanonicalJSON(value interface{}) ([]byte, error) {
	var out bytes.Buffer
	if err := writeCanonical(&out, value); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

// DecodeCanonical parses JSON in the form CanonicalJSON can re-render
// faithfully: numbers keep their literal spelling instead of becoming
// float64.
func DecodeCanonical(raw []byte) (interface{}, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value interface{}
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	// A signed payload is one document. Accepting only the first value would
	// silently discard attacker-controlled bytes after the hashed document.
	var trailing interface{}
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("multiple JSON documents")
		}
		return nil, fmt.Errorf("trailing JSON data: %w", err)
	}
	return value, nil
}

func writeCanonical(out *bytes.Buffer, value interface{}) error {
	switch typed := value.(type) {
	case nil:
		out.WriteString("null")
	case bool:
		if typed {
			out.WriteString("true")
		} else {
			out.WriteString("false")
		}
	case string:
		writeCanonicalString(out, typed)
	case json.Number:
		out.WriteString(typed.String())
	case float64:
		// Only reachable when a caller decoded without UseNumber. Formatted
		// the way encoding/json would, so the common cases still agree.
		out.WriteString(formatFloat(typed))
	case int:
		fmt.Fprintf(out, "%d", typed)
	case int64:
		fmt.Fprintf(out, "%d", typed)
	case []interface{}:
		out.WriteByte('[')
		for index, item := range typed {
			if index > 0 {
				out.WriteByte(',')
			}
			if err := writeCanonical(out, item); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	case map[string]interface{}:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		// Byte order over valid UTF-8 is code point order, which is what
		// Python's sorted() gives.
		sort.Strings(keys)
		out.WriteByte('{')
		for index, key := range keys {
			if index > 0 {
				out.WriteByte(',')
			}
			writeCanonicalString(out, key)
			out.WriteByte(':')
			if err := writeCanonical(out, typed[key]); err != nil {
				return err
			}
		}
		out.WriteByte('}')
	default:
		return fmt.Errorf("cannot canonicalise %T", value)
	}
	return nil
}

func formatFloat(value float64) string {
	rendered, err := json.Marshal(value)
	if err != nil {
		return "0"
	}
	return string(rendered)
}

func writeCanonicalString(out *bytes.Buffer, value string) {
	out.WriteByte('"')
	for _, r := range value {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\n':
			out.WriteString(`\n`)
		case '\r':
			out.WriteString(`\r`)
		case '\t':
			out.WriteString(`\t`)
		case '\b':
			out.WriteString(`\b`)
		case '\f':
			out.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(out, `\u%04x`, r)
				continue
			}
			out.WriteRune(r)
		}
	}
	out.WriteByte('"')
}

// CanonicalFromRaw canonicalises a raw JSON document in one step.
func CanonicalFromRaw(raw []byte) ([]byte, error) {
	value, err := DecodeCanonical(raw)
	if err != nil {
		return nil, fmt.Errorf("not valid JSON: %w", err)
	}
	return CanonicalJSON(value)
}

// Indent renders a value for human output. Verification never depends on it.
func Indent(value interface{}) string {
	rendered, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return fmt.Sprintf("%v", value)
	}
	return strings.TrimSpace(string(rendered))
}
