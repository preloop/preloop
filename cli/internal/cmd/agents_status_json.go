package cmd

import (
	"strings"
	"time"
)

// statusJSONDocument is the allowlisted `agents status --json` payload.
// The agent block reuses discoveryJSON. Never add free-form config, paths,
// tokens, or diagnostic strings to this DTO.
type statusJSONDocument struct {
	Agent       discoveryJSON          `json:"agent"`
	LocalState  *statusLocalJSON       `json:"local_state"`
	RemoteState *statusRemoteJSON      `json:"remote_state"`
	Models      []statusModelJSON      `json:"models"`
	Desktop     map[string]interface{} `json:"desktop"`
}

type statusLocalJSON struct {
	AgentName         string     `json:"agent_name,omitempty"`
	EnrollmentID      string     `json:"enrollment_id,omitempty"`
	ConfigExisted     bool       `json:"config_existed"`
	ManagedServerName string     `json:"managed_server_name,omitempty"`
	AppliedAt         time.Time  `json:"applied_at,omitempty"`
	RestoredAt        *time.Time `json:"restored_at,omitempty"`
	PinModelFamilies  bool       `json:"pin_model_families,omitempty"`
}

type statusRemoteJSON struct {
	Agent       statusRemoteAgentJSON  `json:"agent"`
	Credentials []statusCredentialJSON `json:"credentials"`
	Enrollments []statusEnrollmentJSON `json:"enrollments"`
}

type statusRemoteAgentJSON struct {
	ID                     string   `json:"id,omitempty"`
	LifecycleState         string   `json:"lifecycle_state,omitempty"`
	ActivityStatus         string   `json:"activity_status,omitempty"`
	OnboardingState        string   `json:"onboarding_state,omitempty"`
	LatestModelAlias       string   `json:"latest_model_alias,omitempty"`
	ManagedMCPServers      []string `json:"managed_mcp_servers,omitempty"`
	MCPProxyConfigured     bool     `json:"mcp_proxy_configured,omitempty"`
	ModelGatewayConfigured bool     `json:"model_gateway_configured,omitempty"`
	TotalRequests          int      `json:"total_requests,omitempty"`
	EstimatedCost          float64  `json:"estimated_cost,omitempty"`
}

type statusCredentialJSON struct {
	ID        string `json:"id,omitempty"`
	Status    string `json:"status,omitempty"`
	CreatedAt string `json:"created_at,omitempty"`
	RevokedAt string `json:"revoked_at,omitempty"`
}

type statusEnrollmentJSON struct {
	ID               string                 `json:"id,omitempty"`
	EnrollmentType   string                 `json:"enrollment_type,omitempty"`
	AdapterKey       string                 `json:"adapter_key,omitempty"`
	Status           string                 `json:"status,omitempty"`
	ValidationResult map[string]interface{} `json:"validation_result,omitempty"`
	RestoreAvailable bool                   `json:"restore_available"`
	CreatedAt        string                 `json:"created_at,omitempty"`
	UpdatedAt        string                 `json:"updated_at,omitempty"`
	LastAppliedAt    string                 `json:"last_applied_at,omitempty"`
	LastValidatedAt  string                 `json:"last_validated_at,omitempty"`
	LastRestoredAt   string                 `json:"last_restored_at,omitempty"`
}

type statusModelJSON struct {
	ID                string `json:"id,omitempty"`
	Name              string `json:"name,omitempty"`
	ProviderName      string `json:"provider_name,omitempty"`
	ModelIdentifier   string `json:"model_identifier,omitempty"`
	CredentialType    string `json:"credential_type,omitempty"`
	CredentialsStatus string `json:"credentials_status,omitempty"`
	HasAPIKey         bool   `json:"has_api_key,omitempty"`
	IsDefault         bool   `json:"is_default,omitempty"`
}

func safeStatusJSON(
	agent AgentConfig,
	local *localEnrollmentState,
	remote *managedAgentDetailResponse,
	models []aiModelResponse,
	desktop map[string]interface{},
) statusJSONDocument {
	safeModels := safeStatusModelsJSON(models)
	if safeModels == nil {
		safeModels = []statusModelJSON{}
	}
	return statusJSONDocument{
		Agent:       safeStatusAgentJSON(agent),
		LocalState:  safeStatusLocalJSON(local),
		RemoteState: safeStatusRemoteJSON(remote),
		Models:      safeModels,
		Desktop:     safeStatusDesktopJSON(desktop),
	}
}

func safeStatusAgentJSON(agent AgentConfig) discoveryJSON {
	rows := safeDiscoveryJSON([]AgentConfig{agent})
	if len(rows) == 0 {
		return discoveryJSON{}
	}
	return rows[0]
}

func safeStatusLocalJSON(state *localEnrollmentState) *statusLocalJSON {
	if state == nil {
		return nil
	}
	name := ""
	if _, known := inventoryAppIDs[state.AgentName]; known {
		name = state.AgentName
	}
	local := &statusLocalJSON{
		AgentName:         name,
		EnrollmentID:      allowStatusID(state.EnrollmentID),
		ConfigExisted:     state.ConfigExisted,
		ManagedServerName: allowStatusSlug(state.ManagedServerName),
		PinModelFamilies:  state.PinModelFamilies,
	}
	if !state.AppliedAt.IsZero() {
		local.AppliedAt = state.AppliedAt.UTC()
	}
	if state.RestoredAt != nil && !state.RestoredAt.IsZero() {
		restored := state.RestoredAt.UTC()
		local.RestoredAt = &restored
	}
	return local
}

func safeStatusRemoteJSON(detail *managedAgentDetailResponse) *statusRemoteJSON {
	if detail == nil {
		return nil
	}
	remote := &statusRemoteJSON{
		Agent: statusRemoteAgentJSON{
			ID: allowStatusID(detail.Agent.ID),
			LifecycleState: allowDiscoveryEnum(
				detail.Agent.LifecycleState, "active", "suspended", "decommissioned",
			),
			ActivityStatus: allowDiscoveryEnum(
				detail.Agent.ActivityStatus,
				"active_now", "recently_active", "idle", "ended", "suspended", "decommissioned",
			),
			OnboardingState: allowDiscoveryEnum(
				detail.Agent.OnboardingState,
				"fully_onboarded", "mcp_proxy_only", "gateway_only", "incomplete",
			),
			LatestModelAlias:       allowStatusModelAlias(detail.Agent.LatestModelAlias),
			MCPProxyConfigured:     detail.Agent.MCPProxyConfigured,
			ModelGatewayConfigured: detail.Agent.ModelGatewayConfigured,
			TotalRequests:          detail.Agent.TotalRequests,
			EstimatedCost:          detail.Agent.EstimatedCost,
		},
		Credentials: make([]statusCredentialJSON, 0, len(detail.Credentials)),
		Enrollments: make([]statusEnrollmentJSON, 0, len(detail.Enrollments)),
	}
	for _, name := range detail.Agent.ManagedMCPServers {
		if slug := allowStatusSlug(name); slug != "" {
			remote.Agent.ManagedMCPServers = append(remote.Agent.ManagedMCPServers, slug)
		}
	}
	for _, credential := range detail.Credentials {
		row := statusCredentialJSON{
			ID:        allowStatusID(credential.ID),
			Status:    allowDiscoveryEnum(credential.Status, "active", "revoked"),
			CreatedAt: allowStatusTimestamp(credential.CreatedAt),
			RevokedAt: allowStatusTimestamp(credential.RevokedAt),
		}
		if row == (statusCredentialJSON{}) {
			continue
		}
		remote.Credentials = append(remote.Credentials, row)
	}
	for _, enrollment := range detail.Enrollments {
		remote.Enrollments = append(remote.Enrollments, statusEnrollmentJSON{
			ID: allowStatusID(enrollment.ID),
			EnrollmentType: allowDiscoveryEnum(
				enrollment.EnrollmentType,
				"cli_managed_config", "cli_managed_config_restore",
				"runtime_plugin_control", "runtime_session_bootstrap",
			),
			AdapterKey: allowStatusSlug(enrollment.AdapterKey),
			Status: allowDiscoveryEnum(
				enrollment.Status, "applied", "validated", "restored", "validation_failed",
			),
			ValidationResult: safeStatusValidationJSON(enrollment.ValidationResult),
			RestoreAvailable: enrollment.RestoreAvailable,
			CreatedAt:        allowStatusTimestamp(enrollment.CreatedAt),
			UpdatedAt:        allowStatusTimestamp(enrollment.UpdatedAt),
			LastAppliedAt:    allowStatusTimestamp(enrollment.LastAppliedAt),
			LastValidatedAt:  allowStatusTimestamp(enrollment.LastValidatedAt),
			LastRestoredAt:   allowStatusTimestamp(enrollment.LastRestoredAt),
		})
	}
	return remote
}

func safeStatusModelsJSON(models []aiModelResponse) []statusModelJSON {
	out := make([]statusModelJSON, 0, len(models))
	for _, model := range models {
		row := statusModelJSON{
			ID:              allowStatusID(model.ID),
			Name:            allowStatusText(model.Name),
			ProviderName:    allowStatusSlug(strings.ToLower(strings.TrimSpace(model.ProviderName))),
			ModelIdentifier: allowStatusModelAlias(model.ModelIdentifier),
			CredentialType: allowDiscoveryEnum(
				model.CredentialType,
				"api_key", "durable_api_key", "oauth_anthropic_claude_code", "oauth_openai_codex",
			),
			CredentialsStatus: allowDiscoveryEnum(
				model.CredentialsStatus, "active", "error", "revoked", "unknown", "pending", "failed",
			),
			HasAPIKey: model.HasAPIKey,
			IsDefault: model.IsDefault,
		}
		if row == (statusModelJSON{}) {
			continue
		}
		out = append(out, row)
	}
	return out
}

func safeStatusDesktopJSON(desktop map[string]interface{}) map[string]interface{} {
	if desktop == nil {
		return nil
	}
	out := map[string]interface{}{}
	if installed, ok := desktop["installed"].(bool); ok {
		out["installed"] = installed
	}
	if display := allowStatusDisplay(statusString(desktop["display"])); display != "" {
		out["display"] = display
	}
	if port, ok := allowStatusPort(desktop["vnc_port"]); ok {
		out["vnc_port"] = port
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func safeStatusValidationJSON(raw map[string]interface{}) map[string]interface{} {
	if len(raw) == 0 {
		return nil
	}
	out := map[string]interface{}{}
	copyStatusBool(out, raw,
		"validation_passed",
		"config_parse_ok",
		"preloop_server_present",
		"preloop_url_ok",
		"transport_ok",
		"authorization_header_ok",
		"mcp_config_skipped",
		"gateway_present",
		"gateway_base_url_ok",
		"gateway_provider_ok",
		"gateway_token_ok",
		"gateway_model_configured",
		"model_provider_rewritten",
		"live_validation_supported",
		"live_validation_attempted",
		"live_validation_passed",
		"live_validation_request_logged",
		"live_validation_gateway_rolled_back",
		"control_config_written",
		"control_ws_url_ok",
		"control_bearer_token_ok",
		"control_credential_reference_present",
		"control_managed_agent_id_present",
		"control_adapter_package_ok",
		"control_runtime_principal_id_ok",
		"control_runtime_session_id_present",
		"control_plugin_installed",
		"control_plugin_verified",
		"control_channel_configured",
	)
	if status := allowDiscoveryEnum(
		statusString(raw["live_validation_status"]),
		"passed", "failed", "pending", "not_run", "unsupported",
		"throttled", "upstream_unavailable", "upstream_transient",
	); status != "" {
		out["live_validation_status"] = status
	}
	if alias := allowStatusModelAlias(statusString(raw["live_validation_model_alias"])); alias != "" {
		out["live_validation_model_alias"] = alias
	}
	if alias := allowStatusModelAlias(statusString(raw["gateway_model_alias"])); alias != "" {
		out["gateway_model_alias"] = alias
	}
	if key := allowStatusSlug(statusString(raw["adapter_key"])); key != "" {
		out["adapter_key"] = key
	}
	if status := allowDiscoveryEnum(
		statusString(raw["model_status"]),
		"active", "error", "revoked", "unknown", "pending", "failed",
	); status != "" {
		out["model_status"] = status
	}
	if status := allowDiscoveryEnum(
		statusString(raw["control_plugin_install_status"]),
		"plugin_target_not_found", "runtime_plugin_installer_not_found",
		"plugin_source_build_failed", "install_attempted", "installed_and_verified",
		"installed_not_verified", "managed_sidecar_started", "failed", "registration_failed",
	); status != "" {
		out["control_plugin_install_status"] = status
	}
	if status := allowDiscoveryEnum(
		statusString(raw["control_plugin_verification"]),
		"verified", "not_verified_by_cli", "unsupported_agent", "installed_config_path_missing",
	); status != "" {
		out["control_plugin_verification"] = status
	}
	if code, ok := allowStatusCode(raw["live_validation_status_code"]); ok {
		out["live_validation_status_code"] = code
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func copyStatusBool(dst, src map[string]interface{}, keys ...string) {
	for _, key := range keys {
		if value, ok := src[key].(bool); ok {
			dst[key] = value
		}
	}
}

func statusString(value interface{}) string {
	text, _ := value.(string)
	return text
}

func allowStatusID(value string) string {
	if len(value) != 36 {
		return ""
	}
	for i := 0; i < len(value); i++ {
		switch i {
		case 8, 13, 18, 23:
			if value[i] != '-' {
				return ""
			}
		default:
			if !isStatusHex(value[i]) {
				return ""
			}
		}
	}
	return value
}

func isStatusHex(c byte) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}

func allowStatusSlug(value string) string {
	if value == "" || len(value) > 64 {
		return ""
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_':
		default:
			return ""
		}
	}
	return value
}

func allowStatusModelAlias(value string) string {
	if value == "" || len(value) > 128 {
		return ""
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9',
			c == '-', c == '_', c == '.', c == '/':
		default:
			return ""
		}
	}
	return value
}

func allowStatusText(value string) string {
	value = strings.TrimSpace(value)
	if value == "" || len(value) > 80 || strings.Contains(value, "://") {
		return ""
	}
	if strings.Contains(strings.ToLower(value), "bearer") {
		return ""
	}
	for _, r := range value {
		if r < 0x20 || r == 0x7f {
			return ""
		}
	}
	if !strings.Contains(value, " ") && len(value) > 48 {
		return ""
	}
	return value
}

func allowStatusDisplay(value string) string {
	if len(value) < 2 || len(value) > 6 || value[0] != ':' {
		return ""
	}
	for i := 1; i < len(value); i++ {
		if value[i] < '0' || value[i] > '9' {
			return ""
		}
	}
	return value
}

func allowStatusTimestamp(value string) string {
	if value == "" || len(value) > 40 {
		return ""
	}
	if _, err := time.Parse(time.RFC3339, value); err == nil {
		return value
	}
	if _, err := time.Parse(time.RFC3339Nano, value); err == nil {
		return value
	}
	return ""
}

func allowStatusPort(value interface{}) (int, bool) {
	port, ok := statusNumber(value)
	if !ok || port < 1 || port > 65535 {
		return 0, false
	}
	return port, true
}

func allowStatusCode(value interface{}) (int, bool) {
	code, ok := statusNumber(value)
	if !ok || code < 100 || code > 599 {
		return 0, false
	}
	return code, true
}

func statusNumber(value interface{}) (int, bool) {
	switch typed := value.(type) {
	case int:
		return typed, true
	case int64:
		return int(typed), true
	case float64:
		return int(typed), true
	default:
		return 0, false
	}
}
