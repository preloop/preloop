package cmd

import (
	"io"
	"os"
	"path/filepath"
	"testing"
)

func TestRestoreOpenCodeGatewaySelectionFromOriginalPreservesMCP(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.json")
	original := []byte(`{"model":"moonshotai/kimi-k3"}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to seed OpenCode config: %v", err)
	}
	agent := AgentConfig{Name: "OpenCode", ConfigPath: configPath}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load OpenCode config: %v", err)
	}
	doc["model"] = "preloop/moonshotai/kimi-k3"
	doc["provider"] = map[string]interface{}{
		"preloop": map[string]interface{}{
			"npm": "@ai-sdk/openai-compatible",
			"options": map[string]interface{}{
				"baseURL": "https://preloop.example/openai/v1",
				"apiKey":  "durable-token",
			},
		},
	}
	doc["mcp"] = map[string]interface{}{
		"preloop": map[string]interface{}{
			"type": "remote",
			"url":  "https://preloop.example/mcp/v1",
		},
	}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write managed OpenCode config: %v", err)
	}

	if err := restoreOpenCodeGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreOpenCodeGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored OpenCode config: %v", err)
	}
	if restored["model"] != "moonshotai/kimi-k3" {
		t.Fatalf("expected original OpenCode model restored, got %#v", restored["model"])
	}
	if _, exists := restored["provider"]; exists {
		t.Fatalf("expected injected preloop provider removed, got %#v", restored["provider"])
	}
	if _, ok := restored["mcp"].(map[string]interface{})["preloop"]; !ok {
		t.Fatalf("expected Preloop MCP server to remain present, got %#v", restored)
	}
}

func TestRestoreOpenCodeGatewaySelectionDropsContaminatedModelPin(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.json")
	// Backup captured after a previous managed onboarding: restoring its
	// model pin verbatim would reinstate the failing gateway route.
	original := []byte(`{"model":"preloop/moonshotai/kimi-k3"}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to seed OpenCode config: %v", err)
	}
	agent := AgentConfig{Name: "OpenCode", ConfigPath: configPath}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load OpenCode config: %v", err)
	}
	doc["provider"] = map[string]interface{}{
		"preloop": map[string]interface{}{"npm": "@ai-sdk/openai-compatible"},
	}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write managed OpenCode config: %v", err)
	}

	if err := restoreOpenCodeGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreOpenCodeGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored OpenCode config: %v", err)
	}
	if _, exists := restored["model"]; exists {
		t.Fatalf("expected contaminated preloop model pin dropped, got %#v", restored["model"])
	}
	if _, exists := restored["provider"]; exists {
		t.Fatalf("expected injected preloop provider removed, got %#v", restored["provider"])
	}
}

func TestRestoreHermesGatewaySelectionFromOriginalRestoresModelBlock(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	original := []byte("model:\n  default: gpt-5.4\n  provider: openai\n")
	if err := os.WriteFile(configPath, original, 0600); err != nil {
		t.Fatalf("failed to seed Hermes config: %v", err)
	}
	agent := AgentConfig{Name: "Hermes", ConfigPath: configPath}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load Hermes config: %v", err)
	}
	doc["model"] = map[string]interface{}{
		"provider": "custom",
		"base_url": "https://preloop.example/openai/v1",
		"api_key":  "durable-token",
		"default":  "preloop/openai/gpt-5.4",
	}
	doc["preloop"] = map[string]interface{}{
		"control": map[string]interface{}{"ws_url": "wss://preloop.example/api/v1/agents/control/ws"},
	}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write managed Hermes config: %v", err)
	}

	if err := restoreHermesGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreHermesGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored Hermes config: %v", err)
	}
	model, ok := restored["model"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected restored Hermes model block, got %#v", restored["model"])
	}
	if model["default"] != "gpt-5.4" || model["provider"] != "openai" {
		t.Fatalf("expected original Hermes model selection restored, got %#v", model)
	}
	if _, exists := model["api_key"]; exists {
		t.Fatalf("expected gateway api_key removed, got %#v", model)
	}
	if _, ok := restored["preloop"]; !ok {
		t.Fatalf("expected managed preloop control block to remain, got %#v", restored)
	}
}

func TestRestoreHermesGatewaySelectionDropsContaminatedModelBlock(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.yaml")
	original := []byte(
		"model:\n  provider: custom\n  base_url: https://preloop.example/openai/v1\n  default: preloop/openai/gpt-5.4\n",
	)
	if err := os.WriteFile(configPath, original, 0600); err != nil {
		t.Fatalf("failed to seed Hermes config: %v", err)
	}
	agent := AgentConfig{Name: "Hermes", ConfigPath: configPath}

	if err := restoreHermesGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreHermesGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored Hermes config: %v", err)
	}
	if _, exists := restored["model"]; exists {
		t.Fatalf("expected contaminated Hermes model block dropped, got %#v", restored["model"])
	}
}

func TestRestoreOpenClawGatewaySelectionFromOriginalPreservesMCP(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "openclaw.json")
	original := []byte(`{
  "models": {
    "providers": {
      "anthropic": {"apiKey": "sk-ant-original"}
    }
  },
  "agents": {
    "defaults": {"model": "anthropic/claude-opus-4-6"}
  }
}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to seed OpenClaw config: %v", err)
	}
	agent := AgentConfig{Name: "OpenClaw", ConfigPath: configPath}
	doc, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load OpenClaw config: %v", err)
	}
	doc["models"] = map[string]interface{}{
		"providers": map[string]interface{}{
			"preloop": map[string]interface{}{
				"baseUrl": "https://preloop.example/openai/v1",
				"apiKey":  "durable-token",
			},
		},
	}
	doc["agents"] = map[string]interface{}{
		"defaults": map[string]interface{}{"model": "preloop/anthropic/claude-opus-4-6"},
	}
	doc["mcp"] = map[string]interface{}{
		"servers": map[string]interface{}{
			"preloop": map[string]interface{}{"url": "https://preloop.example/mcp/v1"},
		},
	}
	if err := writeAgentConfigDocument(agent, doc); err != nil {
		t.Fatalf("failed to write managed OpenClaw config: %v", err)
	}

	if err := restoreOpenClawGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreOpenClawGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored OpenClaw config: %v", err)
	}
	providers, _ := asObjectMap(lookupValue(restored, "models", "providers"))
	if _, exists := providers["preloop"]; exists {
		t.Fatalf("expected injected preloop provider removed, got %#v", providers)
	}
	if _, exists := providers["anthropic"]; !exists {
		t.Fatalf("expected original anthropic provider restored, got %#v", providers)
	}
	if model := lookupString(restored, "agents", "defaults", "model"); model != "anthropic/claude-opus-4-6" {
		t.Fatalf("expected original OpenClaw model selector restored, got %#v", model)
	}
	if _, ok := asObjectMap(lookupValue(restored, "mcp", "servers", "preloop")); !ok {
		t.Fatalf("expected Preloop MCP server to remain present, got %#v", restored)
	}
}

func TestRestoreOpenClawGatewaySelectionStripsPreloopFromContaminatedBackup(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "openclaw.json")
	original := []byte(`{
  "models": {
    "providers": {
      "preloop": {"baseUrl": "https://preloop.example/openai/v1"}
    }
  }
}`)
	if err := os.WriteFile(configPath, original, 0644); err != nil {
		t.Fatalf("failed to seed OpenClaw config: %v", err)
	}
	agent := AgentConfig{Name: "OpenClaw", ConfigPath: configPath}

	if err := restoreOpenClawGatewaySelectionFromOriginal(agent, original, io.Discard); err != nil {
		t.Fatalf("restoreOpenClawGatewaySelectionFromOriginal returned error: %v", err)
	}

	restored, err := loadAgentConfigDocument(agent)
	if err != nil {
		t.Fatalf("failed to load restored OpenClaw config: %v", err)
	}
	if _, exists := restored["models"]; exists {
		t.Fatalf("expected contaminated preloop provider stripped, got %#v", restored["models"])
	}
}
