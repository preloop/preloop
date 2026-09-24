package proxy

import (
	"errors"
	"net"
	"net/url"
	"strconv"
	"strings"
)

// Blocked ranges are refused when EGRESS_DENY_PRIVATE is true, unless a
// configured carve-out contains the address. IPv4-mapped IPv6 is checked
// as the embedded IPv4 address.
var blockedNets = mustCIDRs([]string{
	"0.0.0.0/8",
	"10.0.0.0/8",
	"100.64.0.0/10",
	"127.0.0.0/8",
	"169.254.0.0/16",
	"172.16.0.0/12",
	"192.168.0.0/16",
	"224.0.0.0/4",
	"169.254.169.254/32",
	"::1/128",
	"::/128",
	"fc00::/7",
	"fe80::/10",
	"ff00::/8",
	"fd00:ec2::254/128",
})

func mustCIDRs(cidrs []string) []*net.IPNet {
	out := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic(err)
		}
		out = append(out, n)
	}
	return out
}

// canonicalHost percent-decodes, case-folds, and recognizes IP literals,
// including decimal and octal forms browsers accept. ip is non-nil when
// the host is an address and must not be resolved. A parse failure is
// fail-closed: the caller must not dial.
func canonicalHost(host string) (string, net.IP, error) {
	decoded, err := decodeHost(host)
	if err != nil {
		return "", nil, err
	}
	decoded = strings.TrimSpace(decoded)
	if decoded == "" || strings.ContainsAny(decoded, " \t/\\@?#") {
		return "", nil, errors.New("invalid host")
	}
	if strings.HasPrefix(decoded, "[") {
		if !strings.HasSuffix(decoded, "]") || strings.Count(decoded, "]") != 1 {
			return "", nil, errors.New("invalid bracketed host")
		}
		inner := decoded[1 : len(decoded)-1]
		if inner == "" || strings.Contains(inner, "%") {
			return "", nil, errors.New("invalid bracketed host")
		}
		ip := net.ParseIP(inner)
		if ip == nil {
			return "", nil, errors.New("invalid bracketed host")
		}
		return ip.String(), ip, nil
	}
	if strings.Contains(decoded, "%") || strings.Contains(decoded, "*") {
		return "", nil, errors.New("invalid host")
	}
	lower := strings.ToLower(strings.TrimSuffix(decoded, "."))
	if lower == "" {
		return "", nil, errors.New("invalid host")
	}
	if ip := net.ParseIP(lower); ip != nil {
		return ip.String(), ip, nil
	}
	if looksLikeIPv4Literal(lower) {
		ip, err := parseInetAton(lower)
		if err != nil {
			return "", nil, err
		}
		return ip.String(), ip, nil
	}
	if !validHostname(lower) {
		return "", nil, errors.New("invalid host")
	}
	return lower, nil, nil
}

func decodeHost(host string) (string, error) {
	cur := host
	for i := 0; i < 4; i++ {
		if !strings.Contains(cur, "%") {
			return cur, nil
		}
		dec, err := url.PathUnescape(cur)
		if err != nil || dec == cur {
			return "", errors.New("invalid percent-encoding")
		}
		cur = dec
	}
	if strings.Contains(cur, "%") {
		return "", errors.New("invalid percent-encoding")
	}
	return cur, nil
}

func looksLikeIPv4Literal(host string) bool {
	parts := strings.Split(host, ".")
	if len(parts) == 0 || len(parts) > 4 {
		return false
	}
	for _, part := range parts {
		if !ipComponentShape(part) {
			return false
		}
	}
	return true
}

func ipComponentShape(s string) bool {
	if s == "" {
		return false
	}
	if strings.HasPrefix(s, "0x") {
		if len(s) == 2 {
			return true
		}
		for _, c := range s[2:] {
			if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
				return false
			}
		}
		return true
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// parseInetAton accepts the glibc inet_aton forms: a, a.b, a.b.c, a.b.c.d,
// with decimal, 0-prefixed octal, and 0x hex components.
func parseInetAton(host string) (net.IP, error) {
	parts := strings.Split(host, ".")
	if len(parts) < 1 || len(parts) > 4 {
		return nil, errors.New("invalid ip literal")
	}
	nums := make([]uint32, len(parts))
	for i, part := range parts {
		bits := 8
		if i == len(parts)-1 {
			bits = 8 * (5 - len(parts))
		}
		n, err := parseIPComponent(part, bits)
		if err != nil {
			return nil, err
		}
		nums[i] = n
	}
	var v uint32
	switch len(nums) {
	case 1:
		v = nums[0]
	case 2:
		v = nums[0]<<24 | nums[1]
	case 3:
		v = nums[0]<<24 | nums[1]<<16 | nums[2]
	case 4:
		v = nums[0]<<24 | nums[1]<<16 | nums[2]<<8 | nums[3]
	}
	ip := net.IPv4(byte(v>>24), byte(v>>16), byte(v>>8), byte(v)).To4()
	if ip == nil {
		return nil, errors.New("invalid ip literal")
	}
	return ip, nil
}

func parseIPComponent(s string, bits int) (uint32, error) {
	if s == "" || bits < 1 || bits > 32 {
		return 0, errors.New("invalid ip literal")
	}
	base := 10
	switch {
	case strings.HasPrefix(s, "0x"):
		base = 16
		s = s[2:]
	case len(s) > 1 && s[0] == '0':
		base = 8
	}
	if s == "" {
		return 0, errors.New("invalid ip literal")
	}
	n, err := strconv.ParseUint(s, base, 32)
	if err != nil {
		return 0, errors.New("invalid ip literal")
	}
	if bits < 32 && n >= 1<<uint(bits) {
		return 0, errors.New("invalid ip literal")
	}
	return uint32(n), nil
}

func validHostname(host string) bool {
	if host == "" || len(host) > 253 {
		return false
	}
	labels := strings.Split(host, ".")
	for _, label := range labels {
		if len(label) == 0 || len(label) > 63 {
			return false
		}
		if label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, c := range label {
			if (c < 'a' || c > 'z') && (c < '0' || c > '9') && c != '-' {
				return false
			}
		}
	}
	return true
}

func (c Config) originAllowed(scheme, host string, port int) bool {
	for _, origin := range c.Origins {
		if origin.Scheme != scheme || origin.Port != port {
			continue
		}
		if origin.Wildcard {
			if host != origin.Host && strings.HasSuffix(host, "."+origin.Host) {
				return true
			}
			continue
		}
		if host == origin.Host {
			return true
		}
	}
	return false
}

func (c Config) addressBlocked(ip net.IP) bool {
	if ip == nil || !c.DenyPrivate {
		return false
	}
	if c.cidrAllows(ip) {
		return false
	}
	check := ip
	if v4 := ip.To4(); v4 != nil {
		check = v4
	}
	for _, network := range blockedNets {
		if network.Contains(check) {
			return true
		}
	}
	return false
}

func (c Config) cidrAllows(ip net.IP) bool {
	for _, network := range c.AllowPrivateCIDRs {
		if network.Contains(ip) {
			return true
		}
		if v4 := ip.To4(); v4 != nil && network.Contains(v4) {
			return true
		}
	}
	return false
}

func ipStrings(ips []net.IP) []string {
	out := make([]string, 0, len(ips))
	for _, ip := range ips {
		if ip != nil {
			out = append(out, ip.String())
		}
	}
	return out
}
