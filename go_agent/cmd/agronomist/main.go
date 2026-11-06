// Command agronomist is the crop expert's agent, working from an office in town.
//
// It is written in Go (a "different vendor") and speaks the same JSON-over-A2A
// requests as the Python farm agents in farm/. When the pest scout reports an
// infestation, it picks a treatment and orders the sprayer.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"iter"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2aclient"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
	a2aslimrpc "github.com/agntcy/slim-a2a-go/a2aslimrpc/v1"
	slim_bindings "github.com/agntcy/slim-bindings-go/v3"
	slim_rpc "github.com/agntcy/slim-bindings-go/v3/slim_rpc"
)

const selfName = "greenvalley/town/agronomist"

// Must match AGENTS in farm/kit.py.
var agents = map[string]string{
	"sprayer":   "greenvalley/farm/sprayer",
	"assistant": "greenvalley/farm/assistant",
}

type payload = map[string]any

func main() {
	secret := os.Getenv("SLIM_SHARED_SECRET")
	if len(secret) < 32 {
		fmt.Fprintln(os.Stderr, "SLIM_SHARED_SECRET must be set to at least 32 characters (run `task secret` to generate one into .env).")
		os.Exit(1)
	}
	if err := run(envOr("SLIM_URL", "http://localhost:46357"), secret); err != nil {
		slog.Error("agronomist error", "err", err)
		os.Exit(1)
	}
}

func run(slimURL, secret string) error {
	slim_bindings.InitializeWithDefaults()
	svc := slim_bindings.GetGlobalService()

	name, err := slim_bindings.NameFromString(selfName)
	if err != nil {
		return err
	}
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
	ag := &agronomist{
		caller: &caller{factory: a2aclient.NewFactory(
			a2aclient.WithDefaultsDisabled(),
			a2aslimrpc.WithSLIMRPCTransport(app, &connID),
		), clients: map[string]*a2aclient.Client{}},
		meta: payload{
			"id":        "agronomist",
			"title":     "Agronomist",
			"slim_name": selfName,
			"location":  envOr("AGENT_LOCATION", "office in town"),
			"host":      host,
			"lang":      "Go",
		},
	}

	server := slim_rpc.ServerNewWithConnection(app, name, &connID)
	a2aslimrpc.NewHandler(a2asrv.NewHandler(ag)).RegisterWith(server)
	fmt.Printf("Agronomist up: %s via %s · location=%s host=%s lang=Go\n", selfName, slimURL, ag.meta["location"], host)

	served := make(chan error, 1)
	go func() { served <- server.ServeBlocking() }()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	select {
	case err := <-served:
		return err
	case <-ctx.Done():
	}

	fmt.Println("Agronomist stopping...")
	server.ShutdownBlocking()
	_ = app.Unsubscribe(name, &connID)
	_ = svc.Disconnect(connID)
	return nil
}

type agronomist struct {
	caller *caller
	meta   payload

	mu         sync.Mutex
	lastAdvice payload
	cases      int
}

var _ a2asrv.AgentExecutor = (*agronomist)(nil)

func (a *agronomist) handle(ctx context.Context, req payload) (payload, error) {
	switch req["action"] {
	case "status":
		a.mu.Lock()
		defer a.mu.Unlock()
		out := payload{"kind": "agronomist", "last_advice": a.lastAdvice, "cases": a.cases}
		for k, v := range a.meta {
			out[k] = v
		}
		return out, nil

	case "advise":
		gh, _ := req["greenhouse"].(string)
		count, _ := req["count"].(float64)
		product, dose := "neem oil", "light dose"
		if count >= 50 {
			dose = "full dose"
		}
		advice := fmt.Sprintf("spray %s with %s (%s)", strings.ToUpper(gh), product, dose)

		// The agronomist orders the treatment itself; the scout only reported it.
		a.notify(advice, "sprayer")
		order := payload{"action": "spray", "greenhouse": gh, "product": product, "ordered_by": "agronomist"}
		result, err := a.caller.call(ctx, "sprayer", order, 5*time.Second)
		sprayer := "sprayer unreachable"
		if err == nil {
			sprayer, _ = result["say"].(string)
		} else {
			a.notify("sprayer did not answer, the scout will report again", "sprayer")
		}

		a.mu.Lock()
		a.cases++
		a.lastAdvice = payload{"greenhouse": gh, "advice": advice, "at": float64(time.Now().UnixMilli()) / 1000}
		a.mu.Unlock()
		// Not ok when the sprayer is busy or unreachable: the scout asks again.
		accepted, _ := result["ok"].(bool)
		return payload{"ok": err == nil && accepted, "advice": advice, "sprayer": sprayer}, nil
	}
	return nil, fmt.Errorf("unknown action %v", req["action"])
}

// notify tells the dashboard what just happened; it never blocks or fails.
func (a *agronomist) notify(text, to string) {
	go func() {
		event := payload{"action": "event", "from": "agronomist", "to": to, "text": text, "level": "info"}
		_, _ = a.caller.call(context.Background(), "assistant", event, 1500*time.Millisecond)
	}()
}

func (a *agronomist) Execute(ctx context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		if execCtx.StoredTask == nil {
			if !yield(a2a.NewSubmittedTask(execCtx, execCtx.Message), nil) {
				return
			}
		}
		var req payload
		answer, err := payload(nil), json.Unmarshal([]byte(textOf(execCtx.Message)), &req)
		if err == nil {
			answer, err = a.handle(ctx, req)
		}
		if err != nil {
			answer = payload{"ok": false, "error": err.Error()}
		}
		out, _ := json.Marshal(answer)
		if !yield(a2a.NewArtifactEvent(execCtx, a2a.NewTextPart(string(out))), nil) {
			return
		}
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCompleted, nil), nil)
	}
}

func (a *agronomist) Cancel(_ context.Context, execCtx *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		yield(a2a.NewStatusUpdateEvent(execCtx, a2a.TaskStateCanceled, nil), nil)
	}
}

// caller sends JSON requests to other agents by short id, reusing one client
// per target and dropping it after a failure, like FarmAgent.call in Python.
type caller struct {
	factory *a2aclient.Factory
	mu      sync.Mutex
	clients map[string]*a2aclient.Client
}

func (c *caller) call(ctx context.Context, target string, req payload, timeout time.Duration) (payload, error) {
	client, err := c.client(ctx, target)
	if err != nil {
		return nil, err
	}
	body, _ := json.Marshal(req)
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	result, err := client.SendMessage(ctx, &a2a.SendMessageRequest{Message: &a2a.Message{
		ID:    a2a.NewMessageID(),
		Role:  a2a.MessageRoleUser,
		Parts: a2a.ContentParts{a2a.NewTextPart(string(body))},
	}})
	if err != nil {
		c.drop(target)
		return nil, err
	}

	// Python and Go agents answer with a task holding an artifact; the .NET
	// sprayer answers with a plain message. Accept both.
	var out payload
	switch r := result.(type) {
	case *a2a.Task:
		for _, artifact := range r.Artifacts {
			for _, part := range artifact.Parts {
				if text, ok := part.Content.(a2a.Text); ok {
					_ = json.Unmarshal([]byte(text), &out)
				}
			}
		}
	case *a2a.Message:
		_ = json.Unmarshal([]byte(textOf(r)), &out)
	}
	return out, nil
}

func (c *caller) client(ctx context.Context, target string) (*a2aclient.Client, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if client, ok := c.clients[target]; ok {
		return client, nil
	}
	card := &a2a.AgentCard{SupportedInterfaces: []*a2a.AgentInterface{
		a2a.NewAgentInterface(agents[target], a2aslimrpc.SLIMProtocol),
	}}
	client, err := c.factory.CreateFromCard(ctx, card)
	if err != nil {
		return nil, err
	}
	c.clients[target] = client
	return client, nil
}

func (c *caller) drop(target string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if client, ok := c.clients[target]; ok {
		_ = client.Destroy()
		delete(c.clients, target)
	}
}

func textOf(msg *a2a.Message) string {
	if msg == nil {
		return ""
	}
	var b strings.Builder
	for _, part := range msg.Parts {
		if text, ok := part.Content.(a2a.Text); ok {
			b.WriteString(string(text))
		}
	}
	return b.String()
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
