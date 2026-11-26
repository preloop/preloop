// Package redact removes credentials from extension text before it leaves the
// device.
//
// Detection runs the gitleaks default rule set (gitleaks.toml, vendored
// unmodified from the version the repository's secret scan pins) through a
// matcher that follows gitleaks' rule semantics: keyword prefilter, secret
// capture group, entropy floor and allowlists. On top of that sit a few local
// rules for shapes gitleaks leaves alone because they are too noisy for a
// repository scan: any Authorization-style header value, any URL userinfo,
// assignments to secret-looking names whatever the value's entropy, bare
// high-entropy tokens and home directory paths.
//
// Redaction is deterministic: the same input always yields the same output.
// It lowers the chance that a secret is stored; it is not a guarantee.
package redact

import (
	_ "embed"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"sync"
	"unicode"

	toml "github.com/pelletier/go-toml/v2"
)

// RulesVersion names the vendored rule file. It is recorded with every
// upload so a reader can tell which rules produced a stored text.
const RulesVersion = "gitleaks-8.30.1+preloop-1"

// WithheldPrivateKey is the Result.Withheld value for a text that holds a
// private key block. Such a text is never uploaded, redacted or not.
const WithheldPrivateKey = "private_key"

// Local rule types. Gitleaks rules use their own rule id as the type.
const (
	TypeAuthorizationHeader = "authorization-header"
	TypeURLCredentials      = "url-credentials"
	TypeSecretAssignment    = "secret-assignment"
	TypeHighEntropy         = "high-entropy"
	TypeHomePath            = "home-path"
)

//go:embed gitleaks.toml
var gitleaksConfig []byte

// Result is the outcome of redacting one text.
type Result struct {
	// Text is the input with every finding replaced by "[REDACTED:<type>]".
	// Empty when Withheld is set.
	Text string
	// Counts maps a placeholder type to how many times it was written.
	Counts map[string]int
	// Total is the sum of Counts.
	Total int
	// Withheld is non-empty when the text must not be uploaded at all.
	Withheld string
}

// Options carries the context of the text being redacted.
type Options struct {
	// Path is the slash-separated path of the file inside its extension. It
	// feeds the path conditions of gitleaks rules; it is never redacted.
	Path string
	// Home is the profile home directory; occurrences become a placeholder.
	Home string
}

// Placeholder returns the text written in place of a finding of that type.
func Placeholder(kind string) string { return "[REDACTED:" + kind + "]" }

type allowlist struct {
	and       bool
	target    string
	regexes   []*regexp.Regexp
	paths     []*regexp.Regexp
	stopwords []string
}

type rule struct {
	id          string
	regex       *regexp.Regexp
	path        *regexp.Regexp
	secretGroup int
	entropy     float64
	keywords    []string
	allowlists  []allowlist
}

type ruleSet struct {
	global []allowlist
	rules  []rule
}

type rawAllowlist struct {
	Condition   string   `toml:"condition"`
	RegexTarget string   `toml:"regexTarget"`
	Regexes     []string `toml:"regexes"`
	Paths       []string `toml:"paths"`
	Stopwords   []string `toml:"stopwords"`
}

type rawConfig struct {
	Allowlist rawAllowlist `toml:"allowlist"`
	Rules     []struct {
		ID          string         `toml:"id"`
		Regex       string         `toml:"regex"`
		Path        string         `toml:"path"`
		SecretGroup int            `toml:"secretGroup"`
		Entropy     float64        `toml:"entropy"`
		Keywords    []string       `toml:"keywords"`
		Allowlists  []rawAllowlist `toml:"allowlists"`
	} `toml:"rules"`
}

var (
	loadOnce sync.Once
	loaded   *ruleSet
	loadErr  error
)

func compileAll(patterns []string) ([]*regexp.Regexp, error) {
	out := make([]*regexp.Regexp, 0, len(patterns))
	for _, pattern := range patterns {
		compiled, err := regexp.Compile(pattern)
		if err != nil {
			return nil, err
		}
		out = append(out, compiled)
	}
	return out, nil
}

func compileAllowlist(raw rawAllowlist) (allowlist, error) {
	regexes, err := compileAll(raw.Regexes)
	if err != nil {
		return allowlist{}, err
	}
	paths, err := compileAll(raw.Paths)
	if err != nil {
		return allowlist{}, err
	}
	stopwords := make([]string, 0, len(raw.Stopwords))
	for _, word := range raw.Stopwords {
		stopwords = append(stopwords, strings.ToLower(word))
	}
	target := raw.RegexTarget
	if target == "" {
		target = "secret"
	}
	return allowlist{and: strings.EqualFold(raw.Condition, "AND"), target: target, regexes: regexes, paths: paths, stopwords: stopwords}, nil
}

// rules parses and compiles the vendored rule file once per process. A rule
// file that does not compile is a build defect, reported to the caller so the
// collector sends no content at all instead of sending it unredacted.
func rules() (*ruleSet, error) {
	loadOnce.Do(func() {
		var raw rawConfig
		if err := toml.Unmarshal(gitleaksConfig, &raw); err != nil {
			loadErr = fmt.Errorf("redaction rules unreadable: %w", err)
			return
		}
		set := &ruleSet{}
		global, err := compileAllowlist(raw.Allowlist)
		if err != nil {
			loadErr = fmt.Errorf("redaction allowlist invalid: %w", err)
			return
		}
		set.global = []allowlist{global}
		for _, item := range raw.Rules {
			if item.Regex == "" {
				// Path-only rules flag a file by name; they find no text.
				continue
			}
			compiled := rule{id: item.ID, secretGroup: item.SecretGroup, entropy: item.Entropy}
			if compiled.regex, err = regexp.Compile(item.Regex); err != nil {
				loadErr = fmt.Errorf("redaction rule %s invalid: %w", item.ID, err)
				return
			}
			if item.Path != "" {
				if compiled.path, err = regexp.Compile(item.Path); err != nil {
					loadErr = fmt.Errorf("redaction rule %s invalid: %w", item.ID, err)
					return
				}
			}
			for _, keyword := range item.Keywords {
				compiled.keywords = append(compiled.keywords, strings.ToLower(keyword))
			}
			for _, allow := range item.Allowlists {
				list, err := compileAllowlist(allow)
				if err != nil {
					loadErr = fmt.Errorf("redaction rule %s invalid: %w", item.ID, err)
					return
				}
				compiled.allowlists = append(compiled.allowlists, list)
			}
			set.rules = append(set.rules, compiled)
		}
		if len(set.rules) < 150 {
			loadErr = fmt.Errorf("redaction rules incomplete: %d rules", len(set.rules))
			return
		}
		sort.SliceStable(set.rules, func(i, j int) bool { return set.rules[i].id < set.rules[j].id })
		loaded = set
	})
	return loaded, loadErr
}

// RuleCount reports how many gitleaks rules are active, for diagnostics.
func RuleCount() (int, error) {
	set, err := rules()
	if err != nil {
		return 0, err
	}
	return len(set.rules), nil
}

func shannonEntropy(data string) float64 {
	if data == "" {
		return 0
	}
	counts := map[rune]int{}
	total := 0
	for _, char := range data {
		counts[char]++
		total++
	}
	entropy := 0.0
	for _, count := range counts {
		freq := float64(count) / float64(total)
		entropy -= freq * math.Log2(freq)
	}
	return entropy
}

func lineAround(text string, start, end int) string {
	from := strings.LastIndexByte(text[:start], '\n') + 1
	to := strings.IndexByte(text[end:], '\n')
	if to < 0 {
		return text[from:]
	}
	return text[from : end+to]
}

// allowed follows gitleaks for regexes and stopwords: an OR allowlist passes
// on any criterion, an AND allowlist needs every criterion it declares. Path
// criteria are the exception. In a repository scan they skip directories such
// as node_modules; here the collector has already chosen the file, so a file
// name never switches a rule off: an OR allowlist ignores its paths and an AND
// allowlist that needs a path never passes.
func (a allowlist) allowed(secret, match, line string) bool {
	if a.and && len(a.paths) > 0 {
		return false
	}
	checks := 0
	passed := 0
	if len(a.regexes) > 0 {
		checks++
		target := secret
		switch a.target {
		case "match":
			target = match
		case "line":
			target = line
		}
		for _, re := range a.regexes {
			if re.MatchString(target) {
				passed++
				break
			}
		}
	}
	if len(a.stopwords) > 0 {
		checks++
		lower := strings.ToLower(secret)
		for _, word := range a.stopwords {
			if strings.Contains(lower, word) {
				passed++
				break
			}
		}
	}
	if checks == 0 {
		return false
	}
	if a.and {
		return passed == checks
	}
	return passed > 0
}

type span struct {
	start, end int
	kind       string
	priority   int
}

func containsDigit(value string) bool {
	return strings.IndexFunc(value, unicode.IsDigit) >= 0
}

func (set *ruleSet) find(text, path string) []span {
	lower := strings.ToLower(text)
	var spans []span
	for _, r := range set.rules {
		if r.path != nil && !r.path.MatchString(path) {
			continue
		}
		if len(r.keywords) > 0 {
			hit := false
			for _, keyword := range r.keywords {
				if strings.Contains(lower, keyword) {
					hit = true
					break
				}
			}
			if !hit {
				continue
			}
		}
		priority := 0
		if strings.Contains(r.id, "generic") {
			priority = 2
		}
		for _, loc := range r.regex.FindAllStringSubmatchIndex(text, -1) {
			start, end := loc[0], loc[1]
			groups := len(loc)/2 - 1
			if groups >= 1 {
				if r.secretGroup > 0 {
					if r.secretGroup > groups || loc[2*r.secretGroup] < 0 {
						continue
					}
					start, end = loc[2*r.secretGroup], loc[2*r.secretGroup+1]
				} else {
					for group := 1; group <= groups; group++ {
						if loc[2*group] >= 0 && loc[2*group+1] > loc[2*group] {
							start, end = loc[2*group], loc[2*group+1]
							break
						}
					}
				}
			}
			secret := text[start:end]
			if strings.TrimSpace(secret) == "" {
				continue
			}
			if r.entropy != 0 && shannonEntropy(secret) <= r.entropy {
				continue
			}
			if r.id == "generic-api-key" && !containsDigit(secret) {
				continue
			}
			match := text[loc[0]:loc[1]]
			line := lineAround(text, loc[0], loc[1])
			skip := false
			for _, allow := range append(append([]allowlist{}, set.global...), r.allowlists...) {
				if allow.allowed(secret, match, line) {
					skip = true
					break
				}
			}
			if skip {
				continue
			}
			spans = append(spans, span{start: start, end: end, kind: r.id, priority: priority})
		}
	}
	return spans
}

// Text redacts one text. An error means the rule set could not be loaded; the
// caller must then send nothing.
func Text(input string, opts Options) (Result, error) {
	set, err := rules()
	if err != nil {
		return Result{}, err
	}
	if HasPrivateKey(input) {
		return Result{Withheld: WithheldPrivateKey, Counts: map[string]int{}}, nil
	}
	spans := append(set.find(input, opts.Path), localSpans(input, opts)...)
	sort.SliceStable(spans, func(i, j int) bool {
		a, b := spans[i], spans[j]
		if a.start != b.start {
			return a.start < b.start
		}
		if a.priority != b.priority {
			return a.priority < b.priority
		}
		if a.end != b.end {
			return a.end > b.end
		}
		return a.kind < b.kind
	})
	result := Result{Counts: map[string]int{}}
	var out strings.Builder
	cursor := 0
	for i := 0; i < len(spans); {
		current := spans[i]
		j := i + 1
		// Overlapping findings collapse into one placeholder that covers
		// their union, typed by the most specific rule among them.
		for j < len(spans) && spans[j].start < current.end {
			if spans[j].end > current.end {
				current.end = spans[j].end
			}
			if spans[j].priority < current.priority {
				current.kind, current.priority = spans[j].kind, spans[j].priority
			}
			j++
		}
		out.WriteString(input[cursor:current.start])
		out.WriteString(Placeholder(current.kind))
		result.Counts[current.kind]++
		result.Total++
		cursor = current.end
		i = j
	}
	out.WriteString(input[cursor:])
	result.Text = out.String()
	return result, nil
}
