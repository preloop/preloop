package proxy

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	reasonNotAllowlisted = "not_allowlisted"
	reasonPrivate        = "private_address"
	reasonResolve        = "resolve_error"
	reasonInvalid        = "invalid_target"
	reasonSelf           = "proxy_self"
	reasonDial           = "dial_error"
)

// Resolver maps a hostname to the addresses that will be checked. IP
// literals never reach the resolver.
type Resolver interface {
	LookupIP(ctx context.Context, host string) ([]net.IP, error)
}

type stdResolver struct{}

func (stdResolver) LookupIP(ctx context.Context, host string) ([]net.IP, error) {
	return net.DefaultResolver.LookupIP(ctx, "ip", host)
}

// Proxy is an HTTP forward proxy. It dials only addresses it has already
// checked and it does not follow redirects.
type Proxy struct {
	Config      Config
	Resolver    Resolver
	DialContext func(ctx context.Context, network, address string) (net.Conn, error)
	Log         io.Writer

	sem        chan struct{}
	logMu      sync.Mutex
	listenPort int
	listenIP   net.IP
	localIPs   []net.IP
	transport  *http.Transport
}

// New builds a proxy that fails closed when a request cannot be parsed,
// resolved, or matched.
func New(cfg Config) *Proxy {
	if cfg.MaxConns < 1 {
		cfg.MaxConns = defaultMaxConns
	}
	if cfg.DialTimeout <= 0 {
		cfg.DialTimeout = defaultDialTimeout
	}
	if cfg.IdleTimeout <= 0 {
		cfg.IdleTimeout = defaultIdleTimeout
	}
	p := &Proxy{
		Config:   cfg,
		Resolver: stdResolver{},
		Log:      io.Discard,
		sem:      make(chan struct{}, cfg.MaxConns),
		localIPs: localInterfaceIPs(),
	}
	p.transport = &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return p.dial(ctx, network, addr)
		},
		ForceAttemptHTTP2:     false,
		TLSNextProto:          make(map[string]func(string, *tls.Conn) http.RoundTripper),
		DisableKeepAlives:     true,
		ResponseHeaderTimeout: cfg.DialTimeout,
		IdleConnTimeout:       cfg.IdleTimeout,
		ExpectContinueTimeout: time.Second,
	}
	return p
}

// UseListener records the address this process serves so CONNECT back to
// that port is refused even when a carve-out would otherwise allow it.
func (p *Proxy) UseListener(addr net.Addr) {
	if addr == nil {
		return
	}
	host, portStr, err := net.SplitHostPort(addr.String())
	if err != nil {
		return
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return
	}
	p.listenPort = port
	if ip := net.ParseIP(host); ip != nil {
		p.listenIP = ip
	}
}

// HTTPServer is the server main serves. IdleTimeout bounds proxied
// connections that stop sending; dials use Config.DialTimeout.
func (p *Proxy) HTTPServer() *http.Server {
	return &http.Server{
		Handler:                      p,
		IdleTimeout:                  p.Config.IdleTimeout,
		ReadHeaderTimeout:            p.Config.DialTimeout,
		DisableGeneralOptionsHandler: true,
	}
}

// LogStartup writes the private-CIDR carve-outs. Operators need this line
// to see which fixture ranges bypass the private-address check.
func (p *Proxy) LogStartup() {
	p.writeLog(map[string]any{
		"ts":                  time.Now().UTC().Format(time.RFC3339Nano),
		"level":               "info",
		"msg":                 "startup",
		"listen":              p.Config.Listen,
		"deny_private":        p.Config.DenyPrivate,
		"allow_private_cidrs": p.Config.AllowCIDRStrings(),
	})
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case p.sem <- struct{}{}:
		defer func() { <-p.sem }()
	default:
		http.Error(w, "egress_unavailable: max_conns", http.StatusServiceUnavailable)
		return
	}
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}
	if !r.URL.IsAbs() {
		if r.Method == http.MethodGet && r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, "ok")
			return
		}
		p.deny(w, r.Method, r.URL.RequestURI(), reasonInvalid, nil)
		return
	}
	p.handleForward(w, r)
}

func (p *Proxy) handleConnect(w http.ResponseWriter, r *http.Request) {
	target := r.Host
	if target == "" {
		target = r.URL.Host
	}
	host, port, err := splitRequiredPort(target)
	if err != nil {
		p.deny(w, r.Method, target, reasonInvalid, nil)
		return
	}
	scheme := "https"
	if port == 80 {
		scheme = "http"
	}
	ip, resolved, reason := p.authorize(r.Context(), scheme, host, port)
	if reason != "" {
		p.deny(w, r.Method, target, reason, resolved)
		return
	}
	upstream, err := p.dial(r.Context(), "tcp", net.JoinHostPort(ip.String(), strconv.Itoa(port)))
	if err != nil {
		p.deny(w, r.Method, target, reasonDial, resolved)
		return
	}
	if p.Config.LogAllowed {
		p.writeLog(map[string]any{
			"ts":       time.Now().UTC().Format(time.RFC3339Nano),
			"level":    "debug",
			"method":   r.Method,
			"target":   target,
			"resolved": resolved,
		})
	}
	hj, ok := w.(http.Hijacker)
	if !ok {
		upstream.Close()
		p.deny(w, r.Method, target, reasonInvalid, resolved)
		return
	}
	client, buf, err := hj.Hijack()
	if err != nil {
		upstream.Close()
		return
	}
	if _, err := buf.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		client.Close()
		upstream.Close()
		return
	}
	if err := buf.Flush(); err != nil {
		client.Close()
		upstream.Close()
		return
	}
	p.splice(client, buf.Reader, upstream)
}

func (p *Proxy) splice(client net.Conn, pending io.Reader, upstream net.Conn) {
	defer client.Close()
	defer upstream.Close()
	idle := p.Config.IdleTimeout
	errc := make(chan struct{}, 2)
	go func() {
		src := deadlineConn{Conn: client, idle: idle}
		dst := deadlineConn{Conn: upstream, idle: idle}
		_, _ = io.Copy(dst, io.MultiReader(pending, src))
		errc <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(deadlineConn{Conn: client, idle: idle}, deadlineConn{Conn: upstream, idle: idle})
		errc <- struct{}{}
	}()
	<-errc
}

type deadlineConn struct {
	net.Conn
	idle time.Duration
}

func (c deadlineConn) Read(b []byte) (int, error) {
	_ = c.Conn.SetReadDeadline(time.Now().Add(c.idle))
	return c.Conn.Read(b)
}

func (c deadlineConn) Write(b []byte) (int, error) {
	_ = c.Conn.SetWriteDeadline(time.Now().Add(c.idle))
	return c.Conn.Write(b)
}

func (p *Proxy) handleForward(w http.ResponseWriter, r *http.Request) {
	scheme := strings.ToLower(r.URL.Scheme)
	// HTTPS from a browser arrives as CONNECT, which preserves the client
	// SNI bytes. Absolute-form https would be re-wrapped and is refused.
	if scheme != "http" {
		p.deny(w, r.Method, r.URL.String(), reasonInvalid, nil)
		return
	}
	host := r.URL.Hostname()
	port, err := portFromURL(r.URL.Port(), scheme)
	if err != nil {
		p.deny(w, r.Method, r.URL.String(), reasonInvalid, nil)
		return
	}
	if r.Host != "" && !sameAuthority(r.Host, host, port, scheme) {
		p.deny(w, r.Method, r.URL.String(), reasonInvalid, nil)
		return
	}
	canon, _, err := canonicalHost(host)
	if err != nil {
		p.deny(w, r.Method, r.URL.String(), reasonInvalid, nil)
		return
	}
	ip, resolved, reason := p.authorize(r.Context(), scheme, canon, port)
	if reason != "" {
		p.deny(w, r.Method, r.URL.String(), reason, resolved)
		return
	}
	out, err := forwardRequest(r, scheme, canon, port, ip)
	if err != nil {
		p.deny(w, r.Method, r.URL.String(), reasonInvalid, resolved)
		return
	}
	resp, err := p.transport.RoundTrip(out)
	if err != nil {
		p.deny(w, r.Method, r.URL.String(), reasonDial, resolved)
		return
	}
	defer resp.Body.Close()
	copyResponse(w, resp)
}

func forwardRequest(r *http.Request, scheme, host string, port int, ip net.IP) (*http.Request, error) {
	out := r.Clone(r.Context())
	out.RequestURI = ""
	out.URL.Scheme = scheme
	out.URL.Host = net.JoinHostPort(ip.String(), strconv.Itoa(port))
	out.Host = joinHostPort(host, port, scheme)
	stripHopByHop(out.Header)
	out.Header.Del("Host")
	out.TransferEncoding = nil
	// Buffer so the upstream request has one framing. A client-supplied
	// Transfer-Encoding is hop-by-hop and must not be forwarded as-is.
	body, err := io.ReadAll(io.LimitReader(r.Body, 32<<20+1))
	if err != nil || len(body) > 32<<20 {
		return nil, errors.New("bad body")
	}
	if len(body) == 0 {
		// A non-nil empty body is sent as chunked. Use NoBody so the
		// upstream sees Content-Length: 0 instead of a second framing.
		out.Body = http.NoBody
		out.ContentLength = 0
	} else {
		out.Body = io.NopCloser(bytes.NewReader(body))
		out.ContentLength = int64(len(body))
		out.GetBody = func() (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(body)), nil
		}
	}
	if out.URL.Path == "" {
		out.URL.Path = "/"
	}
	return out, nil
}

func copyResponse(w http.ResponseWriter, resp *http.Response) {
	stripHopByHop(resp.Header)
	dst := w.Header()
	for key, values := range resp.Header {
		for _, value := range values {
			dst.Add(key, value)
		}
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func (p *Proxy) authorize(ctx context.Context, scheme, host string, port int) (net.IP, []string, string) {
	canon, literal, err := canonicalHost(host)
	if err != nil {
		return nil, nil, reasonInvalid
	}
	if !p.Config.originAllowed(scheme, canon, port) {
		return nil, nil, reasonNotAllowlisted
	}
	var ips []net.IP
	if literal != nil {
		ips = []net.IP{literal}
	} else {
		lookupCtx, cancel := context.WithTimeout(ctx, p.Config.DialTimeout)
		defer cancel()
		resolved, err := p.Resolver.LookupIP(lookupCtx, canon)
		if err != nil || len(resolved) == 0 {
			return nil, nil, reasonResolve
		}
		ips = resolved
	}
	resolved := ipStrings(ips)
	for _, ip := range ips {
		if p.Config.addressBlocked(ip) {
			return nil, resolved, reasonPrivate
		}
	}
	dialIP := ips[0]
	if p.isSelf(dialIP, port) {
		return nil, resolved, reasonSelf
	}
	return dialIP, resolved, ""
}

func (p *Proxy) isSelf(ip net.IP, port int) bool {
	if p.listenPort == 0 || port != p.listenPort || ip == nil {
		return false
	}
	if ip.IsLoopback() || ip.IsUnspecified() {
		return true
	}
	if p.listenIP != nil && (p.listenIP.IsUnspecified() || p.listenIP.Equal(ip)) {
		return true
	}
	for _, local := range p.localIPs {
		if local.Equal(ip) {
			return true
		}
	}
	return false
}

func (p *Proxy) dial(ctx context.Context, network, address string) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, p.Config.DialTimeout)
	defer cancel()
	if p.DialContext != nil {
		return p.DialContext(ctx, network, address)
	}
	return (&net.Dialer{Timeout: p.Config.DialTimeout}).DialContext(ctx, network, address)
}

func (p *Proxy) deny(w http.ResponseWriter, method, target, reason string, resolved []string) {
	if resolved == nil {
		resolved = []string{}
	}
	p.writeLog(map[string]any{
		"ts":       time.Now().UTC().Format(time.RFC3339Nano),
		"method":   method,
		"target":   target,
		"reason":   reason,
		"resolved": resolved,
	})
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusForbidden)
	_, _ = io.WriteString(w, "egress_denied: "+reason+"\n")
}

func (p *Proxy) writeLog(fields map[string]any) {
	if p.Log == nil {
		return
	}
	enc, err := json.Marshal(fields)
	if err != nil {
		return
	}
	p.logMu.Lock()
	defer p.logMu.Unlock()
	_, _ = p.Log.Write(append(enc, '\n'))
}

func portFromURL(raw, scheme string) (int, error) {
	if raw == "" {
		return defaultPort(scheme), nil
	}
	port, err := strconv.Atoi(raw)
	if err != nil || port < 1 || port > 65535 {
		return 0, errors.New("bad port")
	}
	return port, nil
}

func splitRequiredPort(authority string) (string, int, error) {
	host, portStr, err := net.SplitHostPort(authority)
	if err != nil {
		return "", 0, err
	}
	port, err := strconv.Atoi(portStr)
	if err != nil || port < 1 || port > 65535 {
		return "", 0, errors.New("bad port")
	}
	if host == "" {
		return "", 0, errors.New("missing host")
	}
	return host, port, nil
}

func sameAuthority(headerHost, urlHost string, urlPort int, scheme string) bool {
	host, portStr, err := splitHostOptionalPort(headerHost)
	if err != nil {
		return false
	}
	canonHeader, _, err := canonicalHost(host)
	if err != nil {
		return false
	}
	canonURL, _, err := canonicalHost(urlHost)
	if err != nil {
		return false
	}
	if canonHeader != canonURL {
		return false
	}
	if portStr == "" {
		return true
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return false
	}
	return port == urlPort || (port == defaultPort(scheme) && urlPort == defaultPort(scheme))
}

func joinHostPort(host string, port int, scheme string) string {
	if port == defaultPort(scheme) {
		if strings.Contains(host, ":") {
			return "[" + host + "]"
		}
		return host
	}
	return net.JoinHostPort(host, strconv.Itoa(port))
}

var hopByHop = map[string]struct{}{
	"Connection":          {},
	"Proxy-Connection":    {},
	"Keep-Alive":          {},
	"Proxy-Authenticate":  {},
	"Proxy-Authorization": {},
	"Te":                  {},
	"Trailer":             {},
	"Transfer-Encoding":   {},
	"Upgrade":             {},
}

func stripHopByHop(h http.Header) {
	var extra []string
	for _, item := range h.Values("Connection") {
		for _, name := range strings.Split(item, ",") {
			name = strings.TrimSpace(name)
			if name != "" {
				extra = append(extra, name)
			}
		}
	}
	for name := range hopByHop {
		h.Del(name)
	}
	for _, name := range extra {
		h.Del(name)
	}
}

func localInterfaceIPs() []net.IP {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []net.IP
	for _, iface := range ifaces {
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			switch v := addr.(type) {
			case *net.IPNet:
				out = append(out, v.IP)
			case *net.IPAddr:
				out = append(out, v.IP)
			}
		}
	}
	return out
}
