// Package paths defines REST paths without depending on the HTTP client.
// Version checks and telemetry also use these paths; keeping them separate
// avoids an import cycle with the API client's identity headers.
package paths

// RESTPrefix is the current REST contract prefix. It is independent of the
// server software release and of OAuth, MCP and provider gateway versions.
// A future REST version requires deliberate selection by each client.
const RESTPrefix = "/api/v1"
