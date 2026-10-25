# Stateless allowlist forward proxy. Build from this directory:
#   docker build -t egress-proxy environments/egress-proxy
FROM golang:1.26.10@sha256:5c3f3f28a273c9b61698b2d34f18375929790ab1f1d3835d032d21e2b31eae0c AS build
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
