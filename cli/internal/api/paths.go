package api

import "github.com/preloop/preloop/cli/internal/api/paths"

// RESTPrefix is the path prefix for the current REST contract. Get, Post and
// other HTTP helpers still accept complete paths, including non-REST paths.
const RESTPrefix = paths.RESTPrefix
