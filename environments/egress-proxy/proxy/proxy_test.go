package proxy

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func testProxy(t *testing.T, env map[string]string) (*Proxy, *bytes.Buffer, *httptest.Server) {
	t.Helper()
	cfg, err := Load(func(k string) string { return env[k] })
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	var logs bytes.Buffer
	p := New(cfg)
	p.Log = &logs
	p.Resolver = mapResolver{}
	ts := httptest.NewServer(p)
	t.Cleanup(ts.Close)
	p.UseListener(ts.Listener.Addr())
	return p, &logs, ts
}

type mapResolver map[string][]net.IP

func (m mapResolver) LookupIP(context.Context, string) ([]net.IP, error) {
	return nil, io.EOF
}

func (m mapResolver) lookup(host string) ([]net.IP, error) {
	ips, ok := m[host]
	if !ok || len(ips) == 0 {
		return nil, io.EOF
	}
	return ips, nil
}

type scriptedResolver struct {
	mu    sync.Mutex
	ips   map[string][]net.IP
	err   map[string]error
	calls int
}

func (s *scriptedResolver) LookupIP(_ context.Context, host string) ([]net.IP, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	if err := s.err[host]; err != nil {
		return nil, err
	}
	ips := s.ips[host]
	if len(ips) == 0 {
		return nil, io.EOF
	}
	cp := make([]net.IP, len(ips))
	copy(cp, ips)
	return cp, nil
}

func dialRecorder(t *testing.T, upstream string, got *[]string) func(context.Context, string, string) (net.Conn, error) {
	t.Helper()
	return func(ctx context.Context, network, address string) (net.Conn, error) {
		*got = append(*got, address)
		var d net.Dialer
		return d.DialContext(ctx, network, upstream)
	}
}

func denialLines(t *testing.T, logs *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		out = append(out, rec)
	}
	return out
}

func TestConnectAllowedDialsCheckedIP(t *testing.T) {
	up := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || r.TLS.ServerName != "allowed.example" {
			t.Errorf("SNI = %v", r.TLS)
		}
		_, _ = io.WriteString(w, "tunneled")
	}))
	t.Cleanup(up.Close)

	p, logs, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	var dialed []string
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), &dialed)

	client := &http.Client{Transport: &http.Transport{
		Proxy: http.ProxyURL(mustURL(ts.URL)),
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: true,
			ServerName:         "allowed.example",
		},
	}}
	resp, err := client.Get("https://allowed.example/path")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || string(body) != "tunneled" {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
	if len(dialed) != 1 || dialed[0] != "203.0.113.10:443" {
		t.Fatalf("dialed %v, want the checked IP once", dialed)
	}
	for _, rec := range denialLines(t, logs) {
		if rec["reason"] != nil {
			t.Fatalf("unexpected denial %#v", rec)
		}
	}
}

func TestConnectNotAllowlistedLogsDenial(t *testing.T) {
	p, logs, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed a denied target")
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "evil.example:443")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer resp.Body.Close()
	body := mustBody(t, resp)
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "egress_denied: not_allowlisted") {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
	recs := denialLines(t, logs)
	if len(recs) != 1 || recs[0]["reason"] != reasonNotAllowlisted || recs[0]["method"] != "CONNECT" {
		t.Fatalf("logs %#v", recs)
	}
	if _, ok := recs[0]["resolved"].([]any); !ok {
		t.Fatalf("resolved field %#v", recs[0]["resolved"])
	}
}

func TestDNSRebindingPrivateAddress(t *testing.T) {
	p, logs, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("169.254.169.254")},
	}}
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed a private address")
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "allowed.example:443")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer resp.Body.Close()
	body := mustBody(t, resp)
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "egress_denied: private_address") {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
	recs := denialLines(t, logs)
	if len(recs) != 1 || recs[0]["reason"] != "private_address" {
		t.Fatalf("logs %#v", recs)
	}
	raw, _ := json.Marshal(recs[0]["resolved"])
	if !strings.Contains(string(raw), "169.254.169.254") {
		t.Fatalf("resolved %s", raw)
	}
}

func TestMixedAnswersDenied(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10"), net.ParseIP("10.1.2.3")},
	}}
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed despite a private answer")
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "allowed.example:443")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(mustBody(t, resp), "private_address") {
		t.Fatalf("status %d", resp.StatusCode)
	}
}

func TestRedirectIsNotFollowedAndNextRequestIsChecked(t *testing.T) {
	var hits int
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		if r.Host != "allowed.example" {
			t.Errorf("Host = %q", r.Host)
		}
		w.Header().Set("Location", "http://10.0.0.5/")
		w.WriteHeader(http.StatusFound)
	}))
	t.Cleanup(up.Close)

	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://allowed.example,http://10.0.0.5",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	var dialed []string
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), &dialed)

	client := &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(mustURL(ts.URL))},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	resp, err := client.Get("http://allowed.example/redirect")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusFound || resp.Header.Get("Location") != "http://10.0.0.5/" {
		t.Fatalf("status %d location %q", resp.StatusCode, resp.Header.Get("Location"))
	}
	if hits != 1 || len(dialed) != 1 || dialed[0] != "203.0.113.10:80" {
		t.Fatalf("hits %d dialed %v", hits, dialed)
	}

	resp2, err := client.Get("http://10.0.0.5/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	body := mustBody(t, resp2)
	if resp2.StatusCode != http.StatusForbidden || !strings.Contains(body, "egress_denied: private_address") {
		t.Fatalf("second status %d body %q", resp2.StatusCode, body)
	}
	if hits != 1 {
		t.Fatalf("proxy followed the redirect, hits %d", hits)
	}
}

func TestWildcardOrigin(t *testing.T) {
	up := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(up.Close)
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://*.example.com",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"a.example.com":    {net.ParseIP("203.0.113.10")},
		"example.com":      {net.ParseIP("203.0.113.10")},
		"evil-example.com": {net.ParseIP("203.0.113.10")},
		"b.a.example.com":  {net.ParseIP("203.0.113.11")},
	}}
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), new([]string))

	client := ts.Client()
	client.Transport = &http.Transport{
		Proxy: http.ProxyURL(mustURL(ts.URL)),
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: true,
			ServerName:         "a.example.com",
		},
	}
	resp, err := client.Get("https://a.example.com/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("subdomain status %d", resp.StatusCode)
	}
	for _, host := range []string{"example.com:443", "evil-example.com:443"} {
		denied, err := doConnect(t, ts.URL, host)
		if err != nil {
			t.Fatal(err)
		}
		body := mustBody(t, denied)
		denied.Body.Close()
		if denied.StatusCode != http.StatusForbidden || !strings.Contains(body, "not_allowlisted") {
			t.Fatalf("%s status %d body %q", host, denied.StatusCode, body)
		}
	}
}

func TestAllowPrivateCIDR(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, "fixture")
	}))
	t.Cleanup(up.Close)
	env := map[string]string{
		"EGRESS_ALLOWED_ORIGINS":     "http://fixture.example",
		"EGRESS_ALLOW_PRIVATE_CIDRS": "172.20.0.0/16",
	}
	p, logs, ts := testProxy(t, env)
	p.LogStartup()
	if !strings.Contains(logs.String(), "172.20.0.0/16") {
		t.Fatalf("startup log %s", logs.String())
	}
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"fixture.example": {net.ParseIP("172.20.1.5")},
	}}
	var dialed []string
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), &dialed)
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyURL(mustURL(ts.URL))}}
	resp, err := client.Get("http://fixture.example/x")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK || mustBody(t, resp) != "fixture" {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if len(dialed) != 1 || dialed[0] != "172.20.1.5:80" {
		t.Fatalf("dialed %v", dialed)
	}

	blocked, _, ts2 := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://fixture.example",
	})
	blocked.Resolver = p.Resolver
	blocked.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed without carve-out")
		return nil, io.EOF
	}
	resp2, err := proxyClient(ts2.URL).Get("http://fixture.example/x")
	if err != nil {
		// client follows no proxy error body via Get when proxy returns 403.
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusForbidden {
		t.Fatalf("without carve-out status %d", resp2.StatusCode)
	}
}

func TestHealthzAndProxiedHealthz(t *testing.T) {
	_, _, ts := testProxy(t, nil)
	resp, err := ts.Client().Get(ts.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK || mustBody(t, resp) != "ok" {
		t.Fatalf("health status %d", resp.StatusCode)
	}

	conn, err := net.Dial("tcp", hostPort(ts.URL))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _ = io.WriteString(conn, "GET http://egress-proxy:3128/healthz HTTP/1.1\r\nHost: egress-proxy:3128\r\n\r\n")
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	raw, err := io.ReadAll(conn)
	if err != nil && !strings.Contains(err.Error(), "timeout") && !strings.Contains(err.Error(), "deadline") {
		t.Fatal(err)
	}
	text := string(raw)
	if !strings.Contains(text, "403") || !strings.Contains(text, "egress_denied:") {
		t.Fatalf("proxied healthz response %q", text)
	}
	if strings.Contains(text, "\r\n\r\nok") || strings.HasSuffix(text, "ok") && !strings.Contains(text, "egress_denied") {
		t.Fatalf("proxied healthz was served: %q", text)
	}
}

func TestIPv6LiteralAndBrackets(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://[::1],https://[2001:db8::1],https://169.254.169.254",
	})
	var dialed []string
	p.DialContext = func(_ context.Context, _, address string) (net.Conn, error) {
		dialed = append(dialed, address)
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "[::1]:443")
	if err != nil {
		t.Fatal(err)
	}
	body := mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "private_address") {
		t.Fatalf("loopback status %d body %q", resp.StatusCode, body)
	}

	resp, err = doConnect(t, ts.URL, "[2001:db8::1]:443")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if len(dialed) != 1 || dialed[0] != "[2001:db8::1]:443" {
		t.Fatalf("dialed %v", dialed)
	}

	resp, err = doConnect(t, ts.URL, "[::ffff:169.254.169.254]:443")
	if err != nil {
		t.Fatal(err)
	}
	body = mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "private_address") {
		t.Fatalf("mapped status %d body %q", resp.StatusCode, body)
	}

	raw := rawRequest(t, hostPort(ts.URL), "CONNECT 2001:db8::1:443 HTTP/1.1\r\nHost: 2001:db8::1:443\r\n\r\n")
	if !strings.Contains(raw, "400") && !strings.Contains(raw, "invalid_target") {
		t.Fatalf("unbracketed %q", raw)
	}
}

func TestUnspecifiedAndWeirdIPLiterals(t *testing.T) {
	origins := "http://0.0.0.0,http://2130706433,http://017700000001,http://127.0.0.1,http://0x7f000001"
	p, _, ts := testProxy(t, map[string]string{"EGRESS_ALLOWED_ORIGINS": origins})
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed an obfuscated local address")
		return nil, io.EOF
	}
	for _, target := range []string{
		"http://0.0.0.0/",
		"http://2130706433/",
		"http://017700000001/",
		"http://0x7f000001/",
		"http://0177.0.0.1/",
		"http://127.1/",
	} {
		resp, err := proxyClient(ts.URL).Get(target)
		if err != nil {
			t.Fatal(target, err)
		}
		body := mustBody(t, resp)
		resp.Body.Close()
		if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "egress_denied: private_address") {
			t.Fatalf("%s status %d body %q", target, resp.StatusCode, body)
		}
	}
}

func TestUppercaseAndPercentEncodedHost(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Host != "allowed.example" {
			t.Errorf("Host = %q", r.Host)
		}
		_, _ = io.WriteString(w, "ok")
	}))
	t.Cleanup(up.Close)
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://allowed.example,http://169.254.169.254",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	var dialed []string
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), &dialed)

	upper := rawRequest(t, hostPort(ts.URL), "GET http://ALLOWED.EXAMPLE/ HTTP/1.1\r\nHost: ALLOWED.EXAMPLE\r\n\r\n")
	if !strings.Contains(upper, "200") || !strings.Contains(upper, "ok") {
		t.Fatalf("uppercase response %q dialed %v", upper, dialed)
	}
	if len(dialed) != 1 || dialed[0] != "203.0.113.10:80" {
		t.Fatalf("dialed %v", dialed)
	}
	// Go's server rejects a percent-encoded request-target before the
	// handler. That is fail-closed: nothing is dialed. The policy still
	// decodes the same host if it is presented directly.
	encoded := rawRequest(t, hostPort(ts.URL), "GET http://ALLOWED%2eEXAMPLE/ HTTP/1.1\r\nHost: ALLOWED.EXAMPLE\r\n\r\n")
	if !strings.Contains(encoded, "400") || len(dialed) != 1 {
		t.Fatalf("encoded request %q dialed %v", encoded, dialed)
	}
	_, resolved, reason := p.authorize(context.Background(), "http", "169%2e254%2e169%2e254", 80)
	if reason != reasonPrivate || !strings.Contains(strings.Join(resolved, ","), "169.254.169.254") {
		t.Fatalf("decoded metadata reason %s resolved %v", reason, resolved)
	}
	_, _, reason = p.authorize(context.Background(), "http", "ALLOWED%2eEXAMPLE", 80)
	if reason != "" {
		t.Fatalf("decoded allowed host reason %s", reason)
	}

	evil := rawRequest(t, hostPort(ts.URL), "GET http://allowed.example%2eevil.com/ HTTP/1.1\r\nHost: allowed.example.evil.com\r\n\r\n")
	if len(dialed) != 1 || (!strings.Contains(evil, "400") && !strings.Contains(evil, "not_allowlisted")) {
		t.Fatalf("suffix evasion %q dialed %v", evil, dialed)
	}
	_, _, reason = p.authorize(context.Background(), "http", "allowed.example.evil.com", 80)
	if reason != reasonNotAllowlisted {
		t.Fatalf("decoded suffix reason %s", reason)
	}
	meta := rawRequest(t, hostPort(ts.URL), "GET http://169%2e254%2e169%2e254/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n")
	if len(dialed) != 1 || (!strings.Contains(meta, "400") && !strings.Contains(meta, "private_address")) {
		t.Fatalf("encoded metadata %q dialed %v", meta, dialed)
	}
}

func TestConnectOwnPort(t *testing.T) {
	p, logs, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOW_PRIVATE_CIDRS": "127.0.0.0/8",
	})
	_, portStr, err := net.SplitHostPort(ts.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	port, _ := strconv.Atoi(portStr)
	cfg, err := Load(func(k string) string {
		if k == "EGRESS_ALLOWED_ORIGINS" {
			return "https://127.0.0.1:" + portStr
		}
		if k == "EGRESS_ALLOW_PRIVATE_CIDRS" {
			return "127.0.0.0/8"
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	p.Config = cfg
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed the proxy's own port")
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, net.JoinHostPort("127.0.0.1", portStr))
	if err != nil {
		t.Fatal(err)
	}
	body := mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "egress_denied: proxy_self") {
		t.Fatalf("status %d body %q logs %s port %d", resp.StatusCode, body, logs.String(), port)
	}
}

func TestHopByHopStripped(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	got := make(chan string, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		var buf bytes.Buffer
		tmp := make([]byte, 1024)
		for {
			n, err := conn.Read(tmp)
			if n > 0 {
				buf.Write(tmp[:n])
			}
			if bytes.Contains(buf.Bytes(), []byte("hello")) || err != nil {
				break
			}
		}
		got <- buf.String()
		_, _ = io.WriteString(conn, "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
	}()

	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	p.DialContext = dialRecorder(t, ln.Addr().String(), new([]string))

	req := "POST http://allowed.example/submit HTTP/1.1\r\n" +
		"Host: allowed.example\r\n" +
		"Content-Length: 5\r\n" +
		"Connection: close, X-Smuggle\r\n" +
		"X-Smuggle: GET http://169.254.169.254/ HTTP/1.1\r\n" +
		"Proxy-Authorization: Basic bm90LWEtcmVhbC1zZWNyZXQ=\r\n" +
		"Proxy-Connection: keep-alive\r\n" +
		"Keep-Alive: timeout=5\r\n" +
		"\r\nhello"
	raw := rawRequest(t, hostPort(ts.URL), req)
	if !strings.Contains(raw, "200") {
		t.Fatalf("client response %q", raw)
	}
	select {
	case upstream := <-got:
		for _, banned := range []string{"X-Smuggle", "Proxy-Authorization", "Keep-Alive", "Proxy-Connection", "169.254.169.254"} {
			if strings.Contains(upstream, banned) {
				t.Fatalf("forwarded %q in %q", banned, upstream)
			}
		}
		if !strings.Contains(upstream, "hello") || !strings.Contains(upstream, "Host: allowed.example") {
			t.Fatalf("upstream %q", upstream)
		}
		if strings.Contains(strings.ToLower(upstream), "transfer-encoding") {
			t.Fatalf("forwarded transfer-encoding %q", upstream)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("upstream saw nothing")
	}
}

func TestSmuggledTransferEncodingIsRejected(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	got := make(chan string, 1)
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			got <- ""
			return
		}
		defer conn.Close()
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		buf := make([]byte, 8192)
		n, _ := conn.Read(buf)
		got <- string(buf[:n])
		_, _ = io.WriteString(conn, "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
	}()
	p.DialContext = dialRecorder(t, ln.Addr().String(), new([]string))
	raw := rawRequest(t, hostPort(ts.URL),
		"POST http://allowed.example/ HTTP/1.1\r\n"+
			"Host: allowed.example\r\n"+
			"Content-Length: 44\r\n"+
			"Transfer-Encoding: chunked\r\n"+
			"\r\n0\r\n\r\nGET http://169.254.169.254/ HTTP/1.1\r\n\r\n")
	upstream := ""
	select {
	case upstream = <-got:
	case <-time.After(2 * time.Second):
	}
	rejected := strings.Contains(raw, "400") || strings.Contains(raw, "403")
	if upstream == "" && !rejected {
		t.Fatalf("request was neither rejected nor forwarded: %q", raw)
	}
	if upstream != "" {
		header, _, _ := strings.Cut(upstream, "\r\n\r\n")
		if strings.Contains(strings.ToLower(header), "transfer-encoding") {
			t.Fatalf("forwarded transfer-encoding: %q", upstream)
		}
		if strings.Count(header, "\n") > 0 && strings.Count(header, "HTTP/1.1") != 1 {
			t.Fatalf("smuggled a second request: %q", upstream)
		}
	}
}

func TestResolverErrorFailClosed(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Resolver = &scriptedResolver{err: map[string]error{"allowed.example": io.ErrUnexpectedEOF}}
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		t.Fatal("dialed after a resolver error")
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "allowed.example:443")
	if err != nil {
		t.Fatal(err)
	}
	body := mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "resolve_error") {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
}

func TestEmptyAllowlistDenies(t *testing.T) {
	_, _, ts := testProxy(t, nil)
	resp, err := doConnect(t, ts.URL, "allowed.example:443")
	if err != nil {
		t.Fatal(err)
	}
	body := mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden || !strings.Contains(body, "not_allowlisted") {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
}

func TestMaxConns(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "http://allowed.example",
		"EGRESS_MAX_CONNS":       "1",
	})
	started := make(chan struct{})
	release := make(chan struct{})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	p.DialContext = func(context.Context, string, string) (net.Conn, error) {
		select {
		case <-started:
		default:
			close(started)
		}
		<-release
		return nil, io.EOF
	}
	errc := make(chan error, 1)
	go func() {
		resp, err := proxyClient(ts.URL).Get("http://allowed.example/")
		if resp != nil {
			resp.Body.Close()
		}
		errc <- err
	}()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("first request did not start")
	}
	resp, err := proxyClient(ts.URL).Get("http://allowed.example/other")
	if err != nil {
		t.Fatal(err)
	}
	body := mustBody(t, resp)
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable || !strings.Contains(body, "max_conns") {
		t.Fatalf("status %d body %q", resp.StatusCode, body)
	}
	close(release)
	<-errc
}

func TestLogAllowedConnect(t *testing.T) {
	up := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(up.Close)
	p, logs, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
		"EGRESS_LOG_ALLOWED":     "true",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	p.DialContext = dialRecorder(t, up.Listener.Addr().String(), new([]string))
	client := ts.Client()
	client.Transport = &http.Transport{
		Proxy:           http.ProxyURL(mustURL(ts.URL)),
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true, ServerName: "allowed.example"},
	}
	resp, err := client.Get("https://allowed.example/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	var saw bool
	for _, rec := range denialLines(t, logs) {
		if rec["level"] == "debug" && rec["method"] == "CONNECT" {
			saw = true
		}
	}
	if !saw {
		t.Fatalf("logs %s", logs.String())
	}
}

func TestDialTimeoutContext(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Resolver = &scriptedResolver{ips: map[string][]net.IP{
		"allowed.example": {net.ParseIP("203.0.113.10")},
	}}
	p.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
		deadline, ok := ctx.Deadline()
		if !ok {
			t.Error("dial context has no deadline")
		}
		left := time.Until(deadline)
		if left < 5*time.Second || left > 11*time.Second {
			t.Errorf("dial deadline %s", left)
		}
		return nil, io.EOF
	}
	resp, err := doConnect(t, ts.URL, "allowed.example:443")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
}

func TestIdleTimeoutClosesTunnel(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	clientConn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	upstream, err := ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		clientConn.Close()
		upstream.Close()
	})
	p, _, _ := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS": "https://allowed.example",
	})
	p.Config.IdleTimeout = 50 * time.Millisecond
	done := make(chan struct{})
	go func() {
		p.splice(clientConn, bytes.NewReader(nil), upstream)
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("idle tunnel did not close")
	}
}

func TestServerTimeouts(t *testing.T) {
	cfg, err := Load(func(string) string { return "" })
	if err != nil {
		t.Fatal(err)
	}
	if cfg.DialTimeout != 10*time.Second || cfg.IdleTimeout != 120*time.Second || cfg.MaxConns != 256 || cfg.Listen != ":3128" || !cfg.DenyPrivate {
		t.Fatalf("defaults %#v", cfg)
	}
	srv := New(cfg).HTTPServer()
	if srv.IdleTimeout != 120*time.Second || srv.ReadHeaderTimeout != 10*time.Second {
		t.Fatalf("server timeouts idle %s header %s", srv.IdleTimeout, srv.ReadHeaderTimeout)
	}
}

func TestIPLiteralDoesNotResolve(t *testing.T) {
	p, _, ts := testProxy(t, map[string]string{
		"EGRESS_ALLOWED_ORIGINS":     "http://203.0.113.10",
		"EGRESS_ALLOW_PRIVATE_CIDRS": "",
	})
	p.Resolver = &scriptedResolver{err: map[string]error{"203.0.113.10": io.ErrClosedPipe}}
	var dialed []string
	p.DialContext = func(_ context.Context, _, address string) (net.Conn, error) {
		dialed = append(dialed, address)
		return nil, io.EOF
	}
	resp, err := proxyClient(ts.URL).Get("http://203.0.113.10/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if len(dialed) != 1 || dialed[0] != "203.0.113.10:80" {
		t.Fatalf("dialed %v", dialed)
	}
}

func proxyClient(proxy string) *http.Client {
	return &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(mustURL(proxy))},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func doConnect(t *testing.T, proxyURL, authority string) (*http.Response, error) {
	t.Helper()
	raw := rawRequest(t, hostPort(proxyURL), "CONNECT "+authority+" HTTP/1.1\r\nHost: "+authority+"\r\nConnection: close\r\n\r\n")
	resp, err := http.ReadResponse(bufio.NewReader(strings.NewReader(raw)), nil)
	if err != nil {
		return nil, fmt.Errorf("%w raw %q", err, raw)
	}
	return resp, nil
}

func mustURL(raw string) *url.URL {
	u, err := url.Parse(raw)
	if err != nil {
		panic(err)
	}
	return u
}

func mustBody(t *testing.T, resp *http.Response) string {
	t.Helper()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func hostPort(rawURL string) string {
	u := mustURL(rawURL)
	return u.Host
}

func rawRequest(t *testing.T, addr, payload string) string {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, payload); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	var buf bytes.Buffer
	_, _ = io.Copy(&buf, conn)
	return buf.String()
}

func TestPolicyCanonicalForms(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"2130706433", "127.0.0.1"},
		{"017700000001", "127.0.0.1"},
		{"0x7f000001", "127.0.0.1"},
		{"0177.0.0.1", "127.0.0.1"},
		{"127.1", "127.0.0.1"},
		{"0.0.0.0", "0.0.0.0"},
		{"[::1]", "::1"},
		{"[::ffff:169.254.169.254]", "169.254.169.254"},
		{"ALLOWED.Example", "allowed.example"},
	}
	for _, tc := range cases {
		got, ip, err := canonicalHost(tc.in)
		if err != nil {
			t.Fatalf("%s: %v", tc.in, err)
		}
		if got != tc.want || ip == nil && net.ParseIP(tc.want) != nil {
			t.Fatalf("%s -> %s ip %v", tc.in, got, ip)
		}
	}
	if _, _, err := canonicalHost("127.0.0.08"); err == nil {
		t.Fatal("ambiguous octal was accepted")
	}
	if _, _, err := canonicalHost("%zz"); err == nil {
		t.Fatal("bad encoding was accepted")
	}
}

func TestConfigRejectsGarbage(t *testing.T) {
	if _, err := Load(func(k string) string {
		if k == "EGRESS_ALLOWED_ORIGINS" {
			return "ftp://example.com"
		}
		return ""
	}); err == nil {
		t.Fatal("expected invalid origin")
	}
	if _, err := Load(func(k string) string {
		if k == "EGRESS_ALLOW_PRIVATE_CIDRS" {
			return "not-a-cidr"
		}
		return ""
	}); err == nil {
		t.Fatal("expected invalid cidr")
	}
}
