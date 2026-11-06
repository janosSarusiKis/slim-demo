// Command go_agent is the Go twin of slim_demo/agent.py.
//
// It registers under the same SLIM name (agntcy/demo/home_agent) and answers
// in the same format, so the Python ticker cannot tell which language it is
// talking to, only that the answer now says "Go".
package main

import (
	"context"
	"flag"
	"fmt"
	"iter"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"syscall"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	a2aslimrpc "github.com/agntcy/slim-a2a-go/a2aslimrpc/v1"
	slim_bindings "github.com/agntcy/slim-bindings-go/v3"
	slim_rpc "github.com/agntcy/slim-bindings-go/v3/slim_rpc"
)

func main() {
	name := flag.String("name", "home_agent", "SLIM name component of the agent")
	slimURL := flag.String("slim-url", envOr("SLIM_URL", "http://localhost:46357"), "SLIM node address")
	location := flag.String("location", envOr("AGENT_LOCATION", "cloud"), "label shown in every answer")
	notesDir := flag.String("notes-dir", envOr("NOTES_DIR", "notes/cloud"), "directory with local data the agent reports on")
	flag.Parse()

	secret := os.Getenv("SLIM_SHARED_SECRET")
	if len(secret) < 32 {
		fmt.Fprintln(os.Stderr, "SLIM_SHARED_SECRET must be set to at least 32 characters (run `task secret` to generate one into .env).")
		os.Exit(1)
	}

	if err := run(*name, *slimURL, secret, *location, *notesDir); err != nil {
		slog.Error("agent error", "err", err)
		os.Exit(1)
	}
}

func run(component, slimURL, secret, location, notesDir string) error {
	slim_bindings.InitializeWithDefaults()
	svc := slim_bindings.GetGlobalService()

	name := slim_bindings.NewName("agntcy", "demo", component)
	app, err := svc.CreateAppWithSecret(name, secret)
	if err != nil {
		return fmt.Errorf("create app: %w", err)
	}
	connID, err := svc.Connect(slim_bindings.NewInsecureClientConfig(slimURL))
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	if err := app.Subscribe(name, &connID); err != nil {
		return fmt.Errorf("subscribe: %w", err)
	}

	host, _ := os.Hostname()
	executor := &homeExecutor{location: location, host: host, notesDir: notesDir}
	server := slim_rpc.ServerNewWithConnection(app, name, &connID)
	a2aslimrpc.NewHandler(a2asrv.NewHandler(executor)).RegisterWith(server)

	fmt.Printf("home agent up: agntcy/demo/%s via %s · location=%s host=%s lang=Go\n", component, slimURL, location, host)

	served := make(chan error, 1)
	go func() { served <- server.ServeBlocking() }()

	// Stop cleanly on Ctrl-C and on `docker stop` (SIGTERM), so open sessions
	// are closed and callers reconnect at once instead of waiting for a timeout.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	select {
	case err := <-served:
		return err
	case <-ctx.Done():
	}

	fmt.Println("home agent stopping...")
	server.ShutdownBlocking()
	_ = app.Unsubscribe(name, &connID)
	_ = svc.Disconnect(connID)
	return nil
}

type homeExecutor struct {
	location string
	host     string
	notesDir string
}

var _ a2asrv.AgentExecutor = (*homeExecutor)(nil)

func (e *homeExecutor) Execute(_ context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		if execCtx.StoredTask == nil {
			if !yield(a2a.NewSubmittedTask(execCtx, execCtx.Message), nil) {
				return
			}
		}
		if !yield(a2a.NewArtifactEvent(execCtx, a2a.NewTextPart(e.answer())), nil) {
			return
		}
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCompleted, nil), nil)
	}
}

func (e *homeExecutor) Cancel(_ context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCanceled, nil), nil)
	}
}

// answer mirrors HomeAgent.answer in slim_demo/agent.py.
func (e *homeExecutor) answer() string {
	type note struct {
		path    string
		modTime int64
	}
	var notes []note
	entries, _ := os.ReadDir(e.notesDir)
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		notes = append(notes, note{filepath.Join(e.notesDir, entry.Name()), info.ModTime().UnixNano()})
	}
	sort.Slice(notes, func(i, j int) bool { return notes[i].modTime < notes[j].modTime })

	local := "no notes"
	if len(notes) > 0 {
		data, _ := os.ReadFile(notes[len(notes)-1].path)
		latest, _, _ := strings.Cut(strings.TrimSpace(string(data)), "\n")
		local = fmt.Sprintf("%d notes, latest: %q", len(notes), latest)
	}
	return fmt.Sprintf("%s (%s, Go) · %s", e.location, e.host, local)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
