package cmd

// `preloop agents desktop <agent>`: open a brokered live desktop view (#870)
// and expose it on a loopback port for a native VNC client. It uses the same
// REST route and viewer WebSocket as the console, so the server applies the
// same permission checks, view-only filter, time box and audit.

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
)

var (
	agentsDesktopPort     int
	agentsDesktopDuration time.Duration
)

type desktopViewClient interface {
	Get(path string, result interface{}) error
	Post(path string, body, result interface{}) error
	Delete(path string, result interface{}) error
	Token() string
	BaseURL() string
}

type desktopViewResponse struct {
	ViewID    string `json:"view_id"`
	WSURL     string `json:"ws_url"`
	ExpiresAt string `json:"expires_at"`
	Mode      string `json:"mode"`
	State     string `json:"state"`
}

var agentsDesktopCmd = &cobra.Command{
	Use:   "desktop <agent>",
	Short: "Open an agent's live desktop for a local VNC client (view only)",
	Long: `Open a brokered live view of a managed agent's desktop and listen on
127.0.0.1:<port> so a native VNC client can connect (no password needed).

The view is view only: keyboard, mouse and clipboard input are dropped by the
Preloop API. It ends when the VNC client disconnects, on Ctrl-C, or when the
view expires. Nothing is recorded. The agent is named by id or display name.`,
	Example: `  preloop agents desktop "Browser agent" --port 5901
  vncviewer 127.0.0.1:5901`,
	Args: cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return fmt.Errorf("failed to create API client: %w", err)
		}
		if !client.IsAuthenticated() {
			return errors.New("not authenticated - run 'preloop login' first")
		}
		ctx, stop := signal.NotifyContext(cmd.Context(), os.Interrupt)
		defer stop()
		return runAgentsDesktop(ctx, desktopViewSession{
			client:   client,
			agent:    args[0],
			port:     agentsDesktopPort,
			duration: agentsDesktopDuration,
			out:      cmd.OutOrStdout(),
			dial:     defaultDesktopWSDialer,
		})
	},
}

func init() {
	flags := agentsDesktopCmd.Flags()
	flags.IntVar(&agentsDesktopPort, "port", 5901, "local port to listen on (127.0.0.1 only; 0 picks a free port)")
	flags.DurationVar(&agentsDesktopDuration, "duration", 15*time.Minute, "how long the view lasts (the server caps it)")
	agentsCmd.AddCommand(agentsDesktopCmd)
}

type desktopViewSession struct {
	client   desktopViewClient
	agent    string
	port     int
	duration time.Duration
	out      io.Writer
	dial     desktopWSDialer
	// ready, when set, receives the listener address (tests).
	ready chan<- string
}

var desktopUUIDPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// desktopAgentPageSize is the agents list endpoint's own ceiling.
const desktopAgentPageSize = 100

// desktopAgentMaxPages bounds how far name resolution pages (10,000 agents).
var desktopAgentMaxPages = 100

func resolveDesktopAgentID(client desktopViewClient, value string) (string, error) {
	value = strings.TrimSpace(value)
	if desktopUUIDPattern.MatchString(value) {
		var agent managedAgentSummary
		if err := client.Get(api.RESTPrefix+"/agents/"+url.PathEscape(value), &agent); err != nil {
			return "", fmt.Errorf("failed to find agent %s: %w", value, err)
		}
		return value, nil
	}
	var matches []string
	for page := 0; page < desktopAgentMaxPages; page++ {
		var response managedAgentListResponse
		path := fmt.Sprintf(api.RESTPrefix+"/agents?limit=%d&offset=%d", desktopAgentPageSize, page*desktopAgentPageSize)
		if err := client.Get(path, &response); err != nil {
			return "", fmt.Errorf("failed to list agents: %w", err)
		}
		for _, item := range response.Items {
			if item.ID == value {
				return item.ID, nil
			}
			if strings.EqualFold(item.DisplayName, value) || (len(value) >= 4 && strings.HasPrefix(item.ID, value)) {
				matches = append(matches, item.ID)
			}
		}
		if len(response.Items) < desktopAgentPageSize {
			break
		}
	}
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return "", fmt.Errorf("no agent named %q", value)
	default:
		return "", fmt.Errorf("%q matches %d agents; use the agent id", value, len(matches))
	}
}

func runAgentsDesktop(ctx context.Context, s desktopViewSession) error {
	agentID, err := resolveDesktopAgentID(s.client, s.agent)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", s.port))
	if err != nil {
		return fmt.Errorf("cannot listen on 127.0.0.1:%d: %w", s.port, err)
	}
	defer listener.Close() //nolint:errcheck

	body := map[string]interface{}{}
	if s.duration > 0 {
		body["duration_seconds"] = int(s.duration.Seconds())
	}
	var view desktopViewResponse
	if err := s.client.Post(api.RESTPrefix+"/agents/"+agentID+"/desktop-views", body, &view); err != nil {
		return fmt.Errorf("failed to open the desktop view: %w", err)
	}
	defer func() {
		_ = s.client.Delete(api.RESTPrefix+"/desktop/views/"+view.ViewID, nil)
	}()
	addr := listener.Addr().String()
	fmt.Fprintf(s.out, "Live desktop (view only, nothing is recorded) on %s until %s\n", addr, view.ExpiresAt) //nolint:errcheck
	fmt.Fprintf(s.out, "Connect a VNC client to %s; Ctrl-C ends the view.\n", addr)                            //nolint:errcheck
	if s.ready != nil {
		s.ready <- addr
	}

	accepted := make(chan net.Conn, 1)
	go func() {
		conn, err := listener.Accept()
		if err == nil {
			accepted <- conn
		}
	}()
	var local net.Conn
	select {
	case <-ctx.Done():
		return nil
	case local = <-accepted:
	}
	_ = listener.Close() // one VNC client per view
	defer local.Close()  //nolint:errcheck

	wsURL, err := desktopWSURL(s.client.BaseURL(), view.WSURL)
	if err != nil {
		return err
	}
	header := http.Header{
		"Authorization":          {"Bearer " + s.client.Token()},
		"Sec-WebSocket-Protocol": {"binary"},
	}
	ws, err := s.dial(ctx, wsURL, header)
	if err != nil {
		return fmt.Errorf("failed to connect to the desktop view: %w", err)
	}
	stream := newWSStream(ws)
	go func() {
		<-ctx.Done()
		_ = stream.Close()
		_ = local.Close()
	}()
	relayDesktopStreams(stream, local)
	if code := stream.CloseCode(); code != 0 {
		fmt.Fprintf(s.out, "Desktop view ended (%s)\n", desktopCloseText(code)) //nolint:errcheck
	} else {
		fmt.Fprintln(s.out, "Desktop view ended") //nolint:errcheck
	}
	return nil
}

func desktopCloseText(code int) string {
	switch code {
	case 4403:
		return "not allowed"
	case 4404:
		return "view not found or already ended"
	case 4408:
		return "idle"
	case 4409:
		return "another viewer is attached"
	case 4410:
		return "the agent's desktop disconnected"
	case 4411:
		return "expired"
	case 4412:
		return "closed"
	case 4413:
		return "display unavailable on the host"
	case 4415:
		return "the agent's desktop did not answer"
	default:
		return fmt.Sprintf("code %d", code)
	}
}
