package cmd

// `preloop models smoke <model-alias>` sends one tiny chat completion through
// the Preloop gateway and reports what an operator needs to know after adding
// a model (for example a Bedrock or Azure OpenAI one): did the upstream
// answer, how long did it take, how many tokens were counted, and which usage
// row the Cost page will show for it. It uses the normal gateway route
// (/openai/v1/chat/completions), so it exercises the same credential,
// routing and usage-recording path as an agent would.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/spf13/cobra"
)

const (
	modelsSmokeDefaultPrompt    = "Reply with the single word: ok"
	modelsSmokeDefaultMaxTokens = 32
	modelsSmokeDefaultTimeout   = 120 * time.Second
	modelsSmokeUsageHeader      = "X-Preloop-Usage-Id"
	modelsSmokeWarningHeader    = "X-Preloop-Warning"
	modelsSmokeErrorBodyLimit   = 500
	modelsSmokeReplyLimit       = 120
)

var (
	modelsSmokePrompt    string
	modelsSmokeMaxTokens int
	modelsSmokeTimeout   time.Duration
	modelsSmokeJSON      bool
)

var modelsSmokeCmd = &cobra.Command{
	Use:   "smoke <model-alias>",
	Short: "Send one tiny chat completion through the gateway",
	Long: `Send one small chat completion through the Preloop gateway and report the result.

The model alias is the gateway alias shown on the Models page, for example
bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0 or
azure/my-deployment. The request goes to /openai/v1/chat/completions with
your login session (or --token), so it uses the stored provider credentials
and is recorded like any other gateway request.

Printed: HTTP status, latency, prompt/completion/total tokens, the usage
row id (the row the Cost page counts) and the start of the reply. The
command exits non-zero when the gateway returns an error status.

Examples:
  preloop models smoke azure/my-deployment
  preloop models smoke bedrock/amazon.nova-micro-v1:0 --max-tokens 16
  preloop models smoke azure/my-deployment --json`,
	Args: cobra.ExactArgs(1),
	RunE: runModelsSmoke,
}

func init() {
	modelsSmokeCmd.Flags().StringVar(
		&modelsSmokePrompt, "prompt", modelsSmokeDefaultPrompt,
		"User message to send",
	)
	modelsSmokeCmd.Flags().IntVar(
		&modelsSmokeMaxTokens, "max-tokens", modelsSmokeDefaultMaxTokens,
		"Upper bound on completion tokens",
	)
	modelsSmokeCmd.Flags().DurationVar(
		&modelsSmokeTimeout, "timeout", modelsSmokeDefaultTimeout,
		"Request timeout",
	)
	modelsSmokeCmd.Flags().BoolVar(
		&modelsSmokeJSON, "json", false,
		"Print the result as JSON",
	)
	modelsCmd.AddCommand(modelsSmokeCmd)
}

// modelsSmokeOptions are the inputs of one smoke request.
type modelsSmokeOptions struct {
	Alias     string
	Prompt    string
	MaxTokens int
	JSON      bool
}

// modelsSmokeResult is what the smoke check reports, also its --json shape.
type modelsSmokeResult struct {
	Model            string `json:"model"`
	Status           int    `json:"status"`
	OK               bool   `json:"ok"`
	LatencyMS        int64  `json:"latency_ms"`
	PromptTokens     int    `json:"prompt_tokens"`
	CompletionTokens int    `json:"completion_tokens"`
	TotalTokens      int    `json:"total_tokens"`
	UsageID          string `json:"usage_id,omitempty"`
	UpstreamModel    string `json:"upstream_model,omitempty"`
	Reply            string `json:"reply,omitempty"`
	Warning          string `json:"warning,omitempty"`
	Error            string `json:"error,omitempty"`
}

// modelsSmokeCompletion is the subset of a chat completion the check reads.
type modelsSmokeCompletion struct {
	Model   string `json:"model"`
	Choices []struct {
		Message struct {
			Content json.RawMessage `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage *struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		TotalTokens      int `json:"total_tokens"`
	} `json:"usage"`
}

func runModelsSmoke(cmd *cobra.Command, args []string) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}
	client.SetTimeout(modelsSmokeTimeout)
	return executeModelsSmoke(client, os.Stdout, modelsSmokeOptions{
		Alias:     args[0],
		Prompt:    modelsSmokePrompt,
		MaxTokens: modelsSmokeMaxTokens,
		JSON:      modelsSmokeJSON,
	})
}

// executeModelsSmoke sends the request and renders the result. Split from
// runModelsSmoke so tests can drive it against a fake gateway.
func executeModelsSmoke(client *api.Client, w io.Writer, opts modelsSmokeOptions) error {
	alias := strings.TrimSpace(opts.Alias)
	if alias == "" {
		return fmt.Errorf("a model alias is required")
	}
	prompt := opts.Prompt
	if strings.TrimSpace(prompt) == "" {
		prompt = modelsSmokeDefaultPrompt
	}
	request := map[string]interface{}{
		"model":    alias,
		"messages": []map[string]string{{"role": "user", "content": prompt}},
		"stream":   false,
	}
	if opts.MaxTokens > 0 {
		request["max_tokens"] = opts.MaxTokens
	}

	started := time.Now()
	resp, err := client.PostRaw("/openai/v1/chat/completions", request)
	latency := time.Since(started)
	if err != nil {
		return fmt.Errorf("smoke request for %s failed: %w", alias, err)
	}

	result := modelsSmokeResult{
		Model:     alias,
		Status:    resp.StatusCode,
		OK:        resp.StatusCode >= 200 && resp.StatusCode < 300,
		LatencyMS: latency.Milliseconds(),
		UsageID:   strings.TrimSpace(resp.Header.Get(modelsSmokeUsageHeader)),
		Warning:   strings.TrimSpace(resp.Header.Get(modelsSmokeWarningHeader)),
	}
	if result.OK {
		var completion modelsSmokeCompletion
		if err := json.Unmarshal(resp.Body, &completion); err != nil {
			result.OK = false
			result.Error = fmt.Sprintf("gateway returned an unreadable body: %v", err)
		} else {
			result.UpstreamModel = completion.Model
			if completion.Usage != nil {
				result.PromptTokens = completion.Usage.PromptTokens
				result.CompletionTokens = completion.Usage.CompletionTokens
				result.TotalTokens = completion.Usage.TotalTokens
			}
			if len(completion.Choices) > 0 {
				result.Reply = truncateSmokeText(
					smokeMessageText(completion.Choices[0].Message.Content),
					modelsSmokeReplyLimit,
				)
			}
		}
	} else {
		result.Error = smokeErrorMessage(resp.Body)
	}

	if opts.JSON {
		encoder := json.NewEncoder(w)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(result); err != nil {
			return err
		}
	} else {
		renderModelsSmoke(w, result)
	}
	if !result.OK {
		return fmt.Errorf("smoke check for %s failed (status %d)", alias, result.Status)
	}
	return nil
}

func renderModelsSmoke(w io.Writer, r modelsSmokeResult) {
	fmt.Fprintf(w, "Model:      %s\n", r.Model)                                //nolint:errcheck
	fmt.Fprintf(w, "Status:     %d %s\n", r.Status, http.StatusText(r.Status)) //nolint:errcheck
	fmt.Fprintf(w, "Latency:    %d ms\n", r.LatencyMS)                         //nolint:errcheck
	if !r.OK {
		fmt.Fprintf(w, "Error:      %s\n", r.Error) //nolint:errcheck
		if r.UsageID != "" {
			fmt.Fprintf(w, "Usage row:  %s\n", r.UsageID) //nolint:errcheck
		}
		fmt.Fprintln(w, "✗ Smoke check failed") //nolint:errcheck
		return
	}
	fmt.Fprintf( //nolint:errcheck
		w, "Tokens:     %d prompt, %d completion, %d total\n",
		r.PromptTokens, r.CompletionTokens, r.TotalTokens,
	)
	usage := r.UsageID
	if usage == "" {
		usage = "(not returned by this server)"
	}
	fmt.Fprintf(w, "Usage row:  %s\n", usage) //nolint:errcheck
	if r.UpstreamModel != "" {
		fmt.Fprintf(w, "Upstream:   %s\n", r.UpstreamModel) //nolint:errcheck
	}
	if r.Reply != "" {
		fmt.Fprintf(w, "Reply:      %s\n", r.Reply) //nolint:errcheck
	}
	if r.Warning != "" {
		fmt.Fprintf(w, "Warning:    %s\n", r.Warning) //nolint:errcheck
	}
	if r.TotalTokens == 0 {
		fmt.Fprintln(w, "! The upstream reported no token usage; the Cost page will show 0 tokens for this request.") //nolint:errcheck
	}
	fmt.Fprintln(w, "✓ Smoke check passed") //nolint:errcheck
}

// smokeMessageText reads a message content that is either a string or a
// list of {type, text} parts.
func smokeMessageText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return strings.TrimSpace(text)
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &parts); err == nil {
		joined := make([]string, 0, len(parts))
		for _, part := range parts {
			if part.Text != "" {
				joined = append(joined, part.Text)
			}
		}
		return strings.TrimSpace(strings.Join(joined, " "))
	}
	return ""
}

// smokeErrorMessage extracts an OpenAI-style error message, falling back to
// the (bounded) raw body.
func smokeErrorMessage(body []byte) string {
	var envelope struct {
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
		Detail interface{} `json:"detail"`
	}
	if err := json.Unmarshal(body, &envelope); err == nil {
		if envelope.Error != nil && strings.TrimSpace(envelope.Error.Message) != "" {
			return truncateSmokeText(envelope.Error.Message, modelsSmokeErrorBodyLimit)
		}
		if detail, ok := envelope.Detail.(string); ok && strings.TrimSpace(detail) != "" {
			return truncateSmokeText(detail, modelsSmokeErrorBodyLimit)
		}
	}
	text := strings.TrimSpace(string(body))
	if text == "" {
		return "(empty response body)"
	}
	return truncateSmokeText(text, modelsSmokeErrorBodyLimit)
}

func truncateSmokeText(text string, limit int) string {
	text = strings.Join(strings.Fields(text), " ")
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit-3]) + "..."
}
