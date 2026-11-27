package redact

import (
	"regexp"
	"strings"
	"unicode"
)

// Local rules. They cover shapes the gitleaks rules leave alone because they
// would be too noisy in a repository scan, where a finding is a failure. Here
// a finding only costs a placeholder, so the rules lean towards redacting.

const secretNames = `secret|token|passw(?:or)?d|passphrase|pwd|api[_-]?key|access[_-]?key|private[_-]?key|signing[_-]?key|encryption[_-]?key|session[_-]?key|credentials?|authorization|auth[_-]?key|dsn|connection[_-]?string`

var (
	// The armor is matched loosely on purpose: a key file with odd spacing or
	// a wrapped header line is still a key file.
	privateKeyPattern = regexp.MustCompile(`(?i)-----\s*BEGIN[\sA-Z0-9_-]{0,100}PRIVATE\s+KEY`)
	// PuTTY key files carry no PEM armor.
	puttyKeyPattern = regexp.MustCompile(`(?m)^PuTTY-User-Key-File-\d+:`)

	// Any scheme word may precede the value (Bearer, Basic, OAuth, Bot, a
	// vendor's own), so the scheme is matched by shape, not from a list.
	headerPattern = regexp.MustCompile(`(?i)\b(?:(?:proxy-)?authorization|x-api-key|x-auth-token|x-access-token|x-amz-security-token|api-key|apikey)["']?[ \t]*[:=][ \t]*["']?(?:[A-Za-z][A-Za-z0-9_-]{1,20}[ \t]+)?([^\s"'\\,;` + "`" + `]{6,})`)
	cookiePattern = regexp.MustCompile(`(?i)\b(?:set-cookie|cookie)["']?[ \t]*:[ \t]*["']?([^"'\n` + "`" + `]{8,})`)

	// The password runs to the last "@" that is followed by a host, so a
	// password that itself holds "@" or "/" is covered whole.
	urlPairPattern = regexp.MustCompile(`[a-zA-Z][a-zA-Z0-9+.-]{1,31}://([^\s/@:"'<>` + "`" + `]*:[^\s"'<>` + "`" + `]*)@[A-Za-z0-9\[]`)
	urlUserPattern = regexp.MustCompile(`[a-zA-Z][a-zA-Z0-9+.-]{1,31}://([^\s/@:"'<>` + "`" + `]{16,})@`)
	// host:port/path@version is a URL with a port, not a credential.
	urlPortPattern = regexp.MustCompile(`^\d{1,5}(?:/|$)`)

	// A secret-looking name, up to and including any closing quote.
	assignmentNamePattern = regexp.MustCompile(`(?i)(?:[A-Za-z0-9_.-]*?)(?:` + secretNames + `)[A-Za-z0-9_.-]*["'` + "`" + `]?`)
	// What may sit between the name and its value: an optional type
	// annotation (password: str = ...), then the separator.
	assignmentSeparatorPattern = regexp.MustCompile(`^[ \t]*(?::[ \t]*[A-Za-z_][A-Za-z0-9_.\[\]|, ]{0,40}?[ \t]*(=)|(:=|=>|=|:))[ \t]*`)
	// A value: a string literal with an optional prefix (b"", r'', f""),
	// triple quoted, quoted with escapes or in backticks; or a bare token.
	assignmentValuePattern = regexp.MustCompile(`^(?:[bBrRuUfF]{0,2}(?:"""((?s:.*?))"""|'''((?s:.*?))'''|"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)')|` + "`([^`\\n]*)`" + `|([^\s"'` + "`" + `,;(){}\[\]=>|&][^\s"'` + "`" + `,;)}\]]*))`)
	// A reference in closed brackets right after the separator.
	bracketedReferencePattern = regexp.MustCompile(`^["']?(?:\$\{[^{}\n]*\}|\$\([^()\n]*\)|\{\{[^{}\n]*\}\}|<[^<>\s"'\n]*>)`)
	// Words that follow "name:" in documentation and schemas, not secrets.
	documentationWordPattern = regexp.MustCompile(`(?i)^(?:str|string|int|integer|float|number|bool|boolean|bytes|object|array|list|dict|map|any|text|required|optional|none|null|nil|true|false|yes|no|on|off|enabled|disabled|redacted|hidden|masked|unset|empty|default|n/a|tbd|todo)$`)

	entropyPattern = regexp.MustCompile(`[A-Za-z0-9+/_\-]{24,}`)
	hexPattern     = regexp.MustCompile(`^[0-9a-fA-F]+$`)

	homePattern = regexp.MustCompile(`(?:/Users/|/home/|[A-Za-z]:\\Users\\|[A-Za-z]:/Users/)([^\s/\\"'` + "`" + `:<>|*?]{1,64})`)

	// Values that refer to a secret without holding one. Every bracketed
	// form must be closed: "<S3cret" is a value, "<token>" is a stand-in.
	placeholderPattern = regexp.MustCompile(`(?i)^(?:\$[A-Za-z_][A-Za-z0-9_]*|\$\{[^{}]*\}|\$\([^()]*\)|\$\d+|<[^<>]*>|\{\{[^{}]*\}\}|\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z_][A-Za-z0-9_]*%|\[REDACTED:[a-z0-9-]+\]|x{3,}|\*{3,}|\.{3,}|…|true|false|null|none|nil|undefined|os\.environ\S*|process\.env\S*|env\.\S+|secrets\.\S+)$`)
	numberPattern      = regexp.MustCompile(`^-?\d{1,11}(?:\.\d+)?$`)
)

// isPlaceholder reports values that reference a secret without holding one:
// variable expansions, template fields, angle-bracket stand-ins and literals
// such as true or null. Keeping them is what keeps a redacted skill readable.
// A value such as "$ecret123" cannot be told from a variable reference and is
// kept; that is a stated limit.
func isPlaceholder(value string) bool {
	value = strings.TrimSpace(value)
	return value == "" || placeholderPattern.MatchString(value)
}

// assignmentSpans finds values assigned to secret-looking names. It walks
// name, separator and value in turn so that a type annotation, a string
// prefix or an escaped quote cannot hide the value.
func assignmentSpans(text string) []span {
	var spans []span
	for _, name := range assignmentNamePattern.FindAllStringIndex(text, -1) {
		rest := text[name[1]:]
		separator := assignmentSeparatorPattern.FindStringSubmatchIndex(rest)
		if separator == nil {
			continue
		}
		colon := separator[2] < 0 && rest[separator[4]:separator[5]] == ":"
		after := rest[separator[1]:]
		if bracketedReferencePattern.MatchString(after) {
			// ${VAR}, $(command), {{ field }} and <stand-in> hold no secret.
			continue
		}
		value := assignmentValuePattern.FindStringSubmatchIndex(after)
		if value == nil {
			continue
		}
		start, end, quoted := -1, -1, false
		for group := 1; group <= 6; group++ {
			if value[2*group] >= 0 {
				start, end, quoted = value[2*group], value[2*group+1], group != 6
				break
			}
		}
		if start < 0 || start == end {
			continue
		}
		if !quoted {
			// A bare value that ends a sentence does not include the full stop.
			end = start + len(strings.TrimRight(after[start:end], ".!?:"))
			if start == end {
				continue
			}
		}
		literal := after[start:end]
		// "=~ pattern" is a match operator, not an assignment.
		if !quoted && literal == "~" {
			continue
		}
		if isPlaceholder(literal) || numberPattern.MatchString(literal) {
			continue
		}
		if colon && !quoted {
			tail := after[end:]
			if newline := strings.IndexByte(tail, '\n'); newline >= 0 {
				tail = tail[:newline]
			}
			if strings.TrimSpace(tail) == "" {
				// "name: value" alone on a line is configuration. Only words
				// that describe a field, and do not fill it, are kept.
				if documentationWordPattern.MatchString(literal) {
					continue
				}
			} else {
				// Mid-sentence, prose follows a colon far more often than a
				// secret does; require the value to look like one.
				long := len(literal) >= 16 && shannonEntropy(literal) >= 3.5
				if !(len(literal) >= 8 && containsDigit(literal)) && !long {
					continue
				}
			}
		}
		base := name[1] + separator[1]
		spans = append(spans, span{start: base + start, end: base + end, kind: TypeSecretAssignment, priority: 2})
	}
	return spans
}

func localSpans(text string, opts Options) []span {
	var spans []span
	for _, pattern := range []*regexp.Regexp{headerPattern, cookiePattern} {
		for _, loc := range pattern.FindAllStringSubmatchIndex(text, -1) {
			if !isPlaceholder(text[loc[2]:loc[3]]) {
				spans = append(spans, span{start: loc[2], end: loc[3], kind: TypeAuthorizationHeader, priority: 1})
			}
		}
	}
	for _, loc := range urlPairPattern.FindAllStringSubmatchIndex(text, -1) {
		_, password, _ := strings.Cut(text[loc[2]:loc[3]], ":")
		if isPlaceholder(password) || urlPortPattern.MatchString(password) {
			continue
		}
		spans = append(spans, span{start: loc[2], end: loc[3], kind: TypeURLCredentials, priority: 1})
	}
	for _, loc := range urlUserPattern.FindAllStringSubmatchIndex(text, -1) {
		if !isPlaceholder(text[loc[2]:loc[3]]) {
			spans = append(spans, span{start: loc[2], end: loc[3], kind: TypeURLCredentials, priority: 1})
		}
	}
	spans = append(spans, assignmentSpans(text)...)
	homes := homeSpans(text, opts.Home)
	for _, loc := range entropyPattern.FindAllStringIndex(text, -1) {
		token := text[loc[0]:loc[1]]
		if strings.Count(token, "/") > 2 || insideAny(loc[0], loc[1], homes) {
			continue
		}
		if hexPattern.MatchString(token) {
			if len(token) >= 32 && shannonEntropy(token) >= 3.0 {
				spans = append(spans, span{start: loc[0], end: loc[1], kind: TypeHighEntropy, priority: 3})
			}
			continue
		}
		letters := strings.IndexFunc(token, unicode.IsLetter) >= 0
		if letters && containsDigit(token) && shannonEntropy(token) >= 4.0 {
			spans = append(spans, span{start: loc[0], end: loc[1], kind: TypeHighEntropy, priority: 3})
		}
	}
	spans = append(spans, homes...)
	for _, loc := range homePattern.FindAllStringSubmatchIndex(text, -1) {
		spans = append(spans, span{start: loc[0], end: loc[3], kind: TypeHomePath, priority: 4})
	}
	return spans
}

// homeSpans finds every occurrence of the profile home directory in text.
//
// Args:
//   - text: the text being redacted.
//   - home: the profile home directory; trailing separators are ignored.
//
// Returns:
//   - one home-path span per occurrence, or none for an empty home.
func homeSpans(text, home string) []span {
	home = strings.TrimRight(home, `/\`)
	if len(home) <= 1 {
		return nil
	}
	var spans []span
	for offset := 0; ; {
		index := strings.Index(text[offset:], home)
		if index < 0 {
			return spans
		}
		start := offset + index
		spans = append(spans, span{start: start, end: start + len(home), kind: TypeHomePath, priority: 4})
		offset = start + len(home)
	}
}

// insideAny reports whether [start, end) lies within one of spans.
//
// A token inside the known home directory is a path segment, such as a
// random temporary directory name in a Windows path, whose backslashes the
// entropy pattern does not span. The home span already redacts it, so it
// keeps the home-path type instead of being reported as a high-entropy secret.
func insideAny(start, end int, spans []span) bool {
	for _, s := range spans {
		if start >= s.start && end <= s.end {
			return true
		}
	}
	return false
}

// HasPrivateKey reports whether text holds a private key block header.
func HasPrivateKey(text string) bool {
	return privateKeyPattern.MatchString(text) || puttyKeyPattern.MatchString(text)
}
