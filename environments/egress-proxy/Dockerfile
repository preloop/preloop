# Stateless allowlist forward proxy. Build from this directory:
#   docker build -t egress-proxy environments/egress-proxy
FROM golang:1.26@sha256:6c2a5538f964f1c82f97ad14988bf05de100d922d159d0e398b54c7b0ca0c6c9 AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
COPY proxy ./proxy
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/egress-proxy .

FROM gcr.io/distroless/static:nonroot@sha256:e2e927ec666bae08560abb3c55d0659eceabb657f56b6782ab500a9fc7f555e3
COPY --from=build /out/egress-proxy /egress-proxy
USER nonroot:nonroot
EXPOSE 3128
ENTRYPOINT ["/egress-proxy"]
