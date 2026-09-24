package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/preloop/preloop/environments/egress-proxy/proxy"
)

func main() {
	cfg, err := proxy.Load(os.Getenv)
	if err != nil {
		fmt.Fprintf(os.Stderr, "egress config: %v\n", err)
		os.Exit(1)
	}
	handler := proxy.New(cfg)
	handler.Log = os.Stdout
	handler.LogStartup()

	ln, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		fmt.Fprintf(os.Stderr, "egress listen: %v\n", err)
		os.Exit(1)
	}
	handler.UseListener(ln.Addr())
	server := handler.HTTPServer()

	errc := make(chan error, 1)
	go func() {
		errc <- server.Serve(ln)
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errc:
		if err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, net.ErrClosed) {
			fmt.Fprintf(os.Stderr, "egress serve: %v\n", err)
			os.Exit(1)
		}
	case <-sig:
		ctx, cancel := context.WithTimeout(context.Background(), cfg.DialTimeout)
		defer cancel()
		_ = server.Shutdown(ctx)
	}
}
