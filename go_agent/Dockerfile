# Build from the repo root: docker build -f go_agent/Dockerfile .
FROM golang:1.26 AS build

WORKDIR /src
COPY go_agent/go.mod go_agent/go.sum ./
RUN go mod download
# slim-bindings-go links a prebuilt Rust library; fetch the one for this platform.
RUN go run github.com/agntcy/slim-bindings-go/v3/cmd/slim-bindings-setup

COPY go_agent/cmd ./cmd
RUN CGO_ENABLED=1 go build -o /out/ ./cmd/...

# Same Debian release as the golang image, so the binary finds the glibc it was linked against.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/ /usr/local/bin/
COPY notes /app/notes

WORKDIR /app
ENV GOLANG_PROTOBUF_REGISTRATION_CONFLICT=warn
CMD ["home_agent"]
