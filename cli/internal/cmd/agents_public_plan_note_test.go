package cmd

import "testing"

func TestPublicPlanNote(t *testing.T) {
	tests := []struct {
		name string
		note string
		want string
	}{
		{name: "empty", note: "", want: ""},
		{
			name: "simple",
			note: "Resolved Gemini CLI API key from GEMINI_API_KEY.",
			want: "Resolved Gemini CLI API key from GEMINI_API_KEY.",
		},
		{
			name: "with quotes",
			note: `Resolved key from "env" source.`,
			want: `Resolved key from "env" source.`,
		},
		{
			name: "with newline",
			note: "line one\nline two",
			want: "line one\nline two",
		},
		{
			name: "unicode",
			note: "Resolved from keychain — Claude credentials.",
			want: "Resolved from keychain — Claude credentials.",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := publicPlanNote(tt.note)
			if got != tt.want {
				t.Fatalf("publicPlanNote(%q) = %q, want %q", tt.note, got, tt.want)
			}
		})
	}
}
