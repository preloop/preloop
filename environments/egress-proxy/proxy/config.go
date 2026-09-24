package proxy

import (
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"
)

const (
	defaultListen      = ":3128"
	defaultMaxConns    = 256
	defaultDialTimeout = 10 * time.Second
	defaultIdleTimeout = 120 * time.Second
)

// Origin is one EGRESS_ALLOWED_ORIGINS entry after canonicalization.
type Origin struct {
	Scheme   string
	Host     string
	Wildcard bool
	Port     int
}

// Config is the process configuration. Zero durations are filled by Load.
type Config struct {
	Listen            string
	Origins           []Origin
	DenyPrivate       bool
	AllowPrivateCIDRs []*net.IPNet
	LogAllowed        bool
	MaxConns          int
	DialTimeout       time.Duration
	IdleTimeout       time.Duration
}

// Load reads proxy settings from getenv. An empty allowlist is valid and
// denies every proxied request. Invalid values fail closed.
func Load(getenv func(string) string) (Config, error) {
	cfg := Config{
		Listen:      defaultListen,
		DenyPrivate: true,
		MaxConns:    defaultMaxConns,
		DialTimeout: defaultDialTimeout,
		IdleTimeout: defaultIdleTimeout,
	}
	if v := strings.TrimSpace(getenv("EGRESS_LISTEN")); v != "" {
		cfg.Listen = v
	}
	if _, _, err := splitListen(cfg.Listen); err != nil {
		return Config{}, fmt.Errorf("EGRESS_LISTEN: %w", err)
	}
	origins, err := parseOrigins(getenv("EGRESS_ALLOWED_ORIGINS"))
	if err != nil {
		return Config{}, err
	}
	cfg.Origins = origins
	deny, err := parseBoolDefault(getenv("EGRESS_DENY_PRIVATE"), true)
	if err != nil {
		return Config{}, fmt.Errorf("EGRESS_DENY_PRIVATE: %w", err)
	}
	cfg.DenyPrivate = deny
	cidrs, err := parseCIDRs(getenv("EGRESS_ALLOW_PRIVATE_CIDRS"))
	if err != nil {
		return Config{}, fmt.Errorf("EGRESS_ALLOW_PRIVATE_CIDRS: %w", err)
	}
	cfg.AllowPrivateCIDRs = cidrs
	logAllowed, err := parseBoolDefault(getenv("EGRESS_LOG_ALLOWED"), false)
	if err != nil {
		return Config{}, fmt.Errorf("EGRESS_LOG_ALLOWED: %w", err)
	}
	cfg.LogAllowed = logAllowed
	if raw := strings.TrimSpace(getenv("EGRESS_MAX_CONNS")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 {
			return Config{}, fmt.Errorf("EGRESS_MAX_CONNS: want a positive integer")
		}
		cfg.MaxConns = n
	}
	return cfg, nil
}

func splitListen(listen string) (string, int, error) {
	host, portStr, err := net.SplitHostPort(listen)
	if err != nil {
		return "", 0, err
	}
	port, err := strconv.Atoi(portStr)
	if err != nil || port < 1 || port > 65535 {
		return "", 0, fmt.Errorf("invalid port %q", portStr)
	}
	if host != "" && net.ParseIP(host) == nil && host != "localhost" {
		return "", 0, fmt.Errorf("invalid address %q", listen)
	}
	return host, port, nil
}

func parseOrigins(raw string) ([]Origin, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var out []Origin
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		origin, err := parseOrigin(part)
		if err != nil {
			return nil, fmt.Errorf("EGRESS_ALLOWED_ORIGINS: %w", err)
		}
		out = append(out, origin)
	}
	return out, nil
}

func parseOrigin(raw string) (Origin, error) {
	scheme, rest, ok := strings.Cut(raw, "://")
	if !ok {
		return Origin{}, fmt.Errorf("invalid origin %q", raw)
	}
	scheme = strings.ToLower(scheme)
	if scheme != "http" && scheme != "https" {
		return Origin{}, fmt.Errorf("invalid scheme in %q", raw)
	}
	if rest == "" || strings.Contains(rest, "/") || strings.Contains(rest, "@") || strings.Contains(rest, "?") {
		return Origin{}, fmt.Errorf("invalid origin %q", raw)
	}
	host, port, wildcard, err := splitOriginHost(rest, scheme)
	if err != nil {
		return Origin{}, fmt.Errorf("invalid origin %q: %w", raw, err)
	}
	canon, ip, err := canonicalHost(host)
	if err != nil {
		return Origin{}, fmt.Errorf("invalid origin %q: %w", raw, err)
	}
	if wildcard {
		if ip != nil || !validHostname(canon) {
			return Origin{}, fmt.Errorf("invalid wildcard origin %q", raw)
		}
	}
	return Origin{Scheme: scheme, Host: canon, Wildcard: wildcard, Port: port}, nil
}

func splitOriginHost(rest, scheme string) (string, int, bool, error) {
	wildcard := false
	if strings.HasPrefix(rest, "*.") {
		wildcard = true
		rest = rest[2:]
		if rest == "" || strings.Contains(rest, "*") {
			return "", 0, false, fmt.Errorf("bad wildcard")
		}
	} else if strings.Contains(rest, "*") {
		return "", 0, false, fmt.Errorf("bad wildcard")
	}
	host, portStr, err := splitHostOptionalPort(rest)
	if err != nil {
		return "", 0, false, err
	}
	port := defaultPort(scheme)
	if portStr != "" {
		port, err = strconv.Atoi(portStr)
		if err != nil || port < 1 || port > 65535 {
			return "", 0, false, fmt.Errorf("bad port")
		}
	}
	return host, port, wildcard, nil
}

func splitHostOptionalPort(rest string) (string, string, error) {
	if strings.HasPrefix(rest, "[") {
		end := strings.IndexByte(rest, ']')
		if end < 0 {
			return "", "", fmt.Errorf("bad bracketed host")
		}
		host := rest[:end+1]
		tail := rest[end+1:]
		if tail == "" {
			return host, "", nil
		}
		if !strings.HasPrefix(tail, ":") {
			return "", "", fmt.Errorf("bad bracketed host")
		}
		return host, tail[1:], nil
	}
	if i := strings.LastIndexByte(rest, ':'); i >= 0 {
		port := rest[i+1:]
		if port == "" || !isDigits(port) {
			return "", "", fmt.Errorf("bad port")
		}
		return rest[:i], port, nil
	}
	return rest, "", nil
}

func defaultPort(scheme string) int {
	if scheme == "http" {
		return 80
	}
	return 443
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func parseBoolDefault(raw string, fallback bool) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return fallback, nil
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	default:
		return false, fmt.Errorf("invalid boolean %q", raw)
	}
}

func parseCIDRs(raw string) ([]*net.IPNet, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var out []*net.IPNet
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		_, network, err := net.ParseCIDR(part)
		if err != nil {
			return nil, fmt.Errorf("invalid CIDR %q", part)
		}
		out = append(out, network)
	}
	return out, nil
}

// AllowCIDRStrings returns the configured carve-out ranges for startup logs.
func (c Config) AllowCIDRStrings() []string {
	out := make([]string, 0, len(c.AllowPrivateCIDRs))
	for _, n := range c.AllowPrivateCIDRs {
		out = append(out, n.String())
	}
	return out
}
