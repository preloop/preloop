# Preloop CLI Makefile

# Build variables
BINARY_NAME := preloop
VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")
BUILD_DATE := $(shell date -u +"%Y-%m-%dT%H:%M:%SZ")
LDFLAGS := -ldflags "-X github.com/preloop/preloop/cli/internal/version.Version=$(VERSION) \
	-X github.com/preloop/preloop/cli/internal/version.Commit=$(COMMIT) \
	-X github.com/preloop/preloop/cli/internal/version.BuildDate=$(BUILD_DATE)"

# Go commands
GOCMD := go
GOBUILD := $(GOCMD) build
GOINSTALL := $(GOCMD) install
GOTEST := $(GOCMD) test
GOMOD := $(GOCMD) mod
GOFMT := gofmt

# Directories
BUILD_DIR := build
CMD_DIR := ./cmd/preloop

# Where `make install-local` puts the dev binary. Override with
# `make install-local PREFIX=/opt/preloop` or `BINDIR=/some/bin`.
PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin
# File mode for the installed dev binary. Pass INSTALL_MODE=555 to install
# it already write-protected (see the install-local comment).
INSTALL_MODE ?= 755

# Platforms for cross-compilation
PLATFORMS := linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64

.PHONY: all build build-all install install-local clean test fmt lint tidy help

# Default target
all: build

# Build for current platform
build:
	@echo "Building $(BINARY_NAME) $(VERSION)..."
	@mkdir -p $(BUILD_DIR)
	$(GOBUILD) $(LDFLAGS) -o $(BUILD_DIR)/$(BINARY_NAME) $(CMD_DIR)
	@echo "Built: $(BUILD_DIR)/$(BINARY_NAME)"

# Cross-compile for all platforms
build-all:
	@echo "Building $(BINARY_NAME) $(VERSION) for all platforms..."
	@mkdir -p $(BUILD_DIR)
	@for platform in $(PLATFORMS); do \
		os=$${platform%/*}; \
		arch=$${platform#*/}; \
		output=$(BUILD_DIR)/$(BINARY_NAME)-$$os-$$arch; \
		if [ "$$os" = "windows" ]; then output=$$output.exe; fi; \
		echo "Building $$os/$$arch..."; \
		GOOS=$$os GOARCH=$$arch $(GOBUILD) $(LDFLAGS) -o $$output $(CMD_DIR) || exit 1; \
	done
	@echo "All builds complete!"
	@ls -la $(BUILD_DIR)/

# Install to GOPATH/bin
install:
	@echo "Installing $(BINARY_NAME)..."
	$(GOINSTALL) $(LDFLAGS) $(CMD_DIR)
	@echo "Installed to $(shell go env GOPATH)/bin/$(BINARY_NAME)"

# Install a dev build as the CLI on this machine (default ~/.local/bin/preloop).
#
# This is the only sanctioned way to update a local dev CLI. It always goes
# through install(1), never cp: install unlinks the existing file and creates
# a new one (new inode), so the running system never sees a binary whose
# bytes changed underneath a cached code-signature evaluation. cp writes into
# the existing inode, which is what makes macOS kill the next exec with
# SIGKILL "Code Signature Invalid". Unlinking needs write access to the
# directory, not the file, so a target guarded with `chmod a-w` still
# installs; the fresh file gets INSTALL_MODE (pass 555 to keep the guard).
# `go build -o <target>` is also banned: it skips the version ldflags (the
# binary reports the compiled-in fallback version and looks like a release to
# the update check) and it replaces a read-only target, bypassing the guard.
install-local: build
	@mkdir -p $(BINDIR)
	install -m $(INSTALL_MODE) $(BUILD_DIR)/$(BINARY_NAME) $(BINDIR)/$(BINARY_NAME)
	@echo "Installed: $(BINDIR)/$(BINARY_NAME) ($(VERSION))"

# Clean build artifacts
clean:
	@echo "Cleaning..."
	@rm -rf $(BUILD_DIR)
	@echo "Clean complete"

# Run tests
test:
	@echo "Running tests..."
	$(GOTEST) -v ./...

# Format code
fmt:
	@echo "Formatting code..."
	$(GOFMT) -s -w .

# Run linter (requires golangci-lint)
lint:
	@echo "Running linter..."
	@if command -v golangci-lint >/dev/null 2>&1; then \
		golangci-lint run; \
	else \
		echo "golangci-lint not installed. Run: go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest"; \
	fi

# Tidy dependencies
tidy:
	@echo "Tidying dependencies..."
	$(GOMOD) tidy

# Show help
help:
	@echo "Preloop CLI Makefile"
	@echo ""
	@echo "Usage:"
	@echo "  make build      Build for current platform"
	@echo "  make build-all  Cross-compile for all platforms"
	@echo "  make install    Install to GOPATH/bin"
	@echo "  make install-local  Build and install(1) to BINDIR (default ~/.local/bin)"
	@echo "  make clean      Remove build artifacts"
	@echo "  make test       Run tests"
	@echo "  make fmt        Format code"
	@echo "  make lint       Run linter"
	@echo "  make tidy       Tidy dependencies"
	@echo "  make help       Show this help"
	@echo ""
	@echo "Variables:"
	@echo "  PREFIX=$(PREFIX)"
	@echo "  BINDIR=$(BINDIR)"
	@echo "  INSTALL_MODE=$(INSTALL_MODE)"
	@echo "  VERSION=$(VERSION)"
	@echo "  COMMIT=$(COMMIT)"
	@echo "  BUILD_DATE=$(BUILD_DATE)"
