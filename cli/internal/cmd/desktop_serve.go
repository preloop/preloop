package cmd

// `preloop desktop serve`: the host side of brokered desktop views (#870).
//
// Holds one outbound WebSocket to /api/v1/desktop/endpoints/ws, registers
// this host's display and heartbeats. On desktop.open it dials the loopback
// VNC server, authenticates locally, opens the per-view sibling socket with
// the one-time view token and relays raw RFB bytes. No inbound port is
// opened and the VNC password never leaves the host.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/preloop/preloop/cli/internal/api"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	desktopEndpointsWSPath = "/api/v1/desktop/endpoints/ws"
	desktopViewTokenHeader = "X-Preloop-View-Token"
	desktopTokenEnv        = "PRELOOP_DESKTOP_TOKEN"
	desktopEndpointIDEnv   = "PRELOOP_DESKTOP_ENDPOINT_ID"
	desktopRelayChunk      = 64 * 1024
)

// Timing knobs; tests shorten them.
var (
	desktopReconnectMin = time.Second
	desktopReconnectMax = 30 * time.Second
	desktopDialTimeout  = 15 * time.Second
)

// desktopWSDialer opens a websocket; tests point it at a fake API.
type desktopWSDialer func(ctx context.Context, wsURL string, header http.Header) (*websocket.Conn, error)

func defaultDesktopWSDialer(ctx context.Context, wsURL string, header http.Header) (*websocket.Conn, error) {
	dialer := *websocket.DefaultDialer
	dialer.HandshakeTimeout = desktopDialTimeout
	conn, response, err := dialer.DialContext(ctx, wsURL, header)
	if response != nil && response.Body != nil {
		_ = response.Body.Close()
	}
	return conn, err
}

type desktopServeOptions struct {
	BaseURL        string
	Token          string
	VNCAddr        string
	PasswdFile     string
	Display        string
	StatePath      string
	Hostname       string
	ManagedAgentID string
	RunnerID       string
	Version        string
	Out            io.Writer
	DialWS         desktopWSDialer
	DialVNC        func(ctx context.Context, addr string) (net.Conn, error)
}

type desktopServer struct {
	opts       desktopServeOptions
	endpointID string
	writeMu    sync.Mutex
	viewsMu    sync.Mutex
	views      map[string]context.CancelFunc
	viewsWG    sync.WaitGroup
}

func (o *desktopServeOptions) defaults() error {
	if o.Out == nil {
		o.Out = os.Stdout
	}
	if o.DialWS == nil {
		o.DialWS = defaultDesktopWSDialer
	}
	if o.DialVNC == nil {
		o.DialVNC = func(ctx context.Context, addr string) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(ctx, "tcp", addr)
		}
	}
	if o.VNCAddr == "" {
		o.VNCAddr = fmt.Sprintf("%s:%d", desktopVNCHost, desktopVNCPort)
	}
	if o.Display == "" {
		o.Display = desktopDisplay
	}
	if o.Hostname == "" {
		o.Hostname, _ = os.Hostname()
	}
	if strings.TrimSpace(o.Token) == "" {
		return errors.New("desktop serve needs an agent key or runtime credential: set " +
			desktopTokenEnv + " or write it to ~/.preloop/desktop/token")
	}
	if strings.Contains(o.Token, ".") {
		return errors.New("desktop serve needs an agent key or runtime credential, not a login session token")
	}
	host, _, err := net.SplitHostPort(o.VNCAddr)
	if err != nil {
		return fmt.Errorf("--vnc-addr: %w", err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return errors.New("--vnc-addr must be a loopback address")
	}
	return nil
}

// desktopWSURL turns the API base URL into a ws(s) URL for path.
func desktopWSURL(base, path string) (string, error) {
	parsed, err := url.Parse(strings.TrimRight(base, "/"))
	if err != nil {
		return "", err
	}
	switch parsed.Scheme {
	case "https":
		parsed.Scheme = "wss"
	case "http":
		parsed.Scheme = "ws"
	case "ws", "wss":
	default:
		return "", fmt.Errorf("unsupported API URL scheme %q", parsed.Scheme)
	}
	return parsed.String() + path, nil
}

// loadOrCreateDesktopEndpointID keeps a stable endpoint id in desktop.json,
// preserving the installer's fields.
func loadOrCreateDesktopEndpointID(statePath string) (string, error) {
	doc := map[string]interface{}{}
	if data, err := os.ReadFile(statePath); err == nil {
		if len(strings.TrimSpace(string(data))) > 0 {
			if err := json.Unmarshal(data, &doc); err != nil {
				return "", fmt.Errorf("failed to read %s: %w", statePath, err)
			}
		}
	} else if !os.IsNotExist(err) {
		return "", err
	}
	if id, ok := doc["endpoint_id"].(string); ok && id != "" {
		return id, nil
	}
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	id := "dep_" + hex.EncodeToString(buf)
	doc["endpoint_id"] = id
	if err := os.MkdirAll(filepath.Dir(statePath), 0o700); err != nil {
		return "", err
	}
	if err := writeJSONDocument(statePath, doc); err != nil {
		return "", err
	}
	return id, os.Chmod(statePath, 0o600)
}

func runDesktopServe(ctx context.Context, opts desktopServeOptions) error {
	if err := opts.defaults(); err != nil {
		return err
	}
	endpointID, err := loadOrCreateDesktopEndpointID(opts.StatePath)
	if err != nil {
		return err
	}
	server := &desktopServer{opts: opts, endpointID: endpointID, views: map[string]context.CancelFunc{}}
	// Views are tied to the serve context; wait for them to close cleanly.
	defer server.viewsWG.Wait()
	backoff := desktopReconnectMin
	for {
		started := time.Now()
		err := server.session(ctx)
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			fmt.Fprintf(opts.Out, "desktop endpoint disconnected: %v\n", err) //nolint:errcheck
		}
		if time.Since(started) > desktopReconnectMax {
			backoff = desktopReconnectMin
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > desktopReconnectMax {
			backoff = desktopReconnectMax
		}
	}
}

func (s *desktopServer) authHeader() http.Header {
	return http.Header{"Authorization": {"Bearer " + s.opts.Token}}
}

func (s *desktopServer) writeJSON(conn *websocket.Conn, v interface{}) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	_ = conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return conn.WriteJSON(v)
}

type desktopControlMessage struct {
	Type              string `json:"type"`
	ID                string `json:"id"`
	EndpointID        string `json:"endpoint_id"`
	HeartbeatInterval int    `json:"heartbeat_interval_seconds"`
	ViewID            string `json:"view_id"`
	ViewToken         string `json:"view_token"`
	Reason            string `json:"reason"`
	Code              string `json:"code"`
	Message           string `json:"message"`
}

func (s *desktopServer) session(ctx context.Context) error {
	wsURL, err := desktopWSURL(s.opts.BaseURL, desktopEndpointsWSPath)
	if err != nil {
		return err
	}
	conn, err := s.opts.DialWS(ctx, wsURL, s.authHeader())
	if err != nil {
		return err
	}
	defer conn.Close() //nolint:errcheck
	register := map[string]interface{}{
		"type":        "desktop.register",
		"endpoint_id": s.endpointID,
		"hostname":    s.opts.Hostname,
		"display":     s.opts.Display,
		"vnc_addr":    s.opts.VNCAddr,
		"version":     s.opts.Version,
	}
	if s.opts.ManagedAgentID != "" {
		register["managed_agent_id"] = s.opts.ManagedAgentID
	}
	if s.opts.RunnerID != "" {
		register["runner_id"] = s.opts.RunnerID
	}
	if err := s.writeJSON(conn, register); err != nil {
		return err
	}
	var first desktopControlMessage
	if err := conn.ReadJSON(&first); err != nil {
		return err
	}
	if first.Type != "desktop.registered" {
		return fmt.Errorf("registration refused: %s %s", first.Code, first.Message)
	}
	interval := time.Duration(first.HeartbeatInterval) * time.Second
	if interval <= 0 {
		interval = 20 * time.Second
	}
	fmt.Fprintf(s.opts.Out, "desktop endpoint %s online (display %s)\n", s.endpointID, s.opts.Display) //nolint:errcheck

	sessionCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-sessionCtx.Done():
				_ = conn.Close()
				return
			case <-ticker.C:
				if err := s.writeJSON(conn, map[string]string{"type": "desktop.heartbeat"}); err != nil {
					_ = conn.Close()
					return
				}
			}
		}
	}()
	for {
		var message desktopControlMessage
		if err := conn.ReadJSON(&message); err != nil {
			return err
		}
		switch message.Type {
		case "desktop.open":
			s.viewsWG.Add(1)
			go func(message desktopControlMessage) {
				defer s.viewsWG.Done()
				s.openView(sessionCtx, conn, message)
			}(message)
		case "desktop.close":
			s.cancelView(message.ViewID)
		}
	}
}

func (s *desktopServer) cancelView(viewID string) {
	s.viewsMu.Lock()
	defer s.viewsMu.Unlock()
	if cancel, ok := s.views[viewID]; ok {
		cancel()
		delete(s.views, viewID)
	}
}

func (s *desktopServer) openView(ctx context.Context, control *websocket.Conn, message desktopControlMessage) {
	viewCtx, cancel := context.WithCancel(ctx)
	s.viewsMu.Lock()
	s.views[message.ViewID] = cancel
	s.viewsMu.Unlock()
	defer s.cancelView(message.ViewID)

	fail := func(code string, err error) {
		fmt.Fprintf(s.opts.Out, "desktop view %s failed: %s\n", message.ViewID, code) //nolint:errcheck
		_ = s.writeJSON(control, map[string]interface{}{
			"type":    "desktop.open_result",
			"view_id": message.ViewID,
			"ok":      false,
			"error":   code,
			"message": safeDesktopError(err),
		})
	}
	var password []byte
	if s.opts.PasswdFile != "" {
		pw, err := readVNCPasswordFile(s.opts.PasswdFile)
		if err != nil && !os.IsNotExist(err) {
			fail("display_unavailable", errors.New("cannot read the VNC password file"))
			return
		}
		password = pw
	}
	vnc, err := s.opts.DialVNC(viewCtx, s.opts.VNCAddr)
	if err != nil {
		fail("display_unavailable", err)
		return
	}
	defer vnc.Close() //nolint:errcheck
	_ = vnc.SetDeadline(time.Now().Add(desktopDialTimeout))
	if err := rfbClientHandshake(vnc, password); err != nil {
		fail("display_unavailable", err)
		return
	}
	_ = vnc.SetDeadline(time.Time{})

	viewURL, err := desktopWSURL(
		s.opts.BaseURL,
		api.RESTPrefix+"/desktop/endpoints/"+url.PathEscape(s.endpointID)+"/views/"+url.PathEscape(message.ViewID),
	)
	if err != nil {
		fail("display_unavailable", err)
		return
	}
	header := s.authHeader()
	header.Set(desktopViewTokenHeader, message.ViewToken)
	ws, err := s.opts.DialWS(viewCtx, viewURL, header)
	if err != nil {
		fmt.Fprintf(s.opts.Out, "desktop view %s: relay connect failed\n", message.ViewID) //nolint:errcheck
		return
	}
	stream := newWSStream(ws)
	defer stream.Close() //nolint:errcheck
	go func() {
		<-viewCtx.Done()
		_ = stream.Close()
		_ = vnc.Close()
	}()
	if err := rfbServeNoneHandshake(stream); err != nil {
		return
	}
	fmt.Fprintf(s.opts.Out, "desktop view %s live\n", message.ViewID) //nolint:errcheck
	relayDesktopStreams(stream, vnc)
	fmt.Fprintf(s.opts.Out, "desktop view %s ended\n", message.ViewID) //nolint:errcheck
}

// safeDesktopError keeps error text short and generic enough to send upstream.
func safeDesktopError(err error) string {
	if err == nil {
		return ""
	}
	text := err.Error()
	if len(text) > 200 {
		text = text[:200]
	}
	return text
}

// relayDesktopStreams pipes both directions until either side ends.
func relayDesktopStreams(a, b io.ReadWriteCloser) {
	done := make(chan struct{}, 2)
	pipe := func(dst io.Writer, src io.Reader) {
		buf := make([]byte, desktopRelayChunk)
		_, _ = io.CopyBuffer(dst, src, buf)
		done <- struct{}{}
	}
	go pipe(a, b)
	go pipe(b, a)
	<-done
	_ = a.Close()
	_ = b.Close()
	<-done // both pumps have stopped reading once this returns
}

// wsStream adapts a binary websocket to a byte stream.
type wsStream struct {
	conn    *websocket.Conn
	readBuf []byte
	errMu   sync.Mutex
	readErr error
	writeMu sync.Mutex
	once    sync.Once
}

func newWSStream(conn *websocket.Conn) *wsStream {
	return &wsStream{conn: conn}
}

func (w *wsStream) Read(p []byte) (int, error) {
	for len(w.readBuf) == 0 {
		kind, data, err := w.conn.ReadMessage()
		if err != nil {
			w.errMu.Lock()
			w.readErr = err
			w.errMu.Unlock()
			return 0, io.EOF
		}
		if kind != websocket.BinaryMessage {
			continue
		}
		w.readBuf = data
	}
	n := copy(p, w.readBuf)
	w.readBuf = w.readBuf[n:]
	return n, nil
}

// CloseCode is the close code the peer sent, or 0. Valid after Read ends.
func (w *wsStream) CloseCode() int {
	w.errMu.Lock()
	err := w.readErr
	w.errMu.Unlock()
	var closeErr *websocket.CloseError
	if errors.As(err, &closeErr) {
		return closeErr.Code
	}
	return 0
}

func (w *wsStream) Write(p []byte) (int, error) {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	if err := w.conn.WriteMessage(websocket.BinaryMessage, p); err != nil {
		return 0, err
	}
	return len(p), nil
}

func (w *wsStream) Close() error {
	w.once.Do(func() {
		w.writeMu.Lock()
		_ = w.conn.WriteControl(
			websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""),
			time.Now().Add(time.Second),
		)
		w.writeMu.Unlock()
		_ = w.conn.Close()
	})
	return nil
}
