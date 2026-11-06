// The sprayer: the spray rig that applies treatments in a greenhouse.
//
// Written in .NET, as if the spray rig's manufacturer shipped it. It speaks the
// same JSON-over-A2A requests as the Python agents in farm/ (see farm/kit.py)
// and the Go agronomist: it takes spray orders, then drives the greenhouse's
// spray nozzles.

using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using A2A;
using Microsoft.Extensions.Logging.Abstractions;
using SlimA2A;

const string SelfName = "greenvalley/farm/sprayer";

var endpoint = Environment.GetEnvironmentVariable("SLIM_URL") ?? "http://localhost:46357";
var secret = Environment.GetEnvironmentVariable("SLIM_SHARED_SECRET") ?? "";
if (secret.Length < 32)
{
    Console.Error.WriteLine("SLIM_SHARED_SECRET must be set to at least 32 characters (run `task secret` to generate one into .env).");
    return 1;
}

await using var slim = await SlimA2AConnection.ConnectAsync(new SlimA2AConnectionOptions { Endpoint = endpoint });
var caller = new Caller(slim, secret);
var sprayer = new Sprayer.SprayerAgent(caller, new JsonObject
{
    ["id"] = "sprayer",
    ["title"] = "Sprayer",
    ["slim_name"] = SelfName,
    ["location"] = Environment.GetEnvironmentVariable("AGENT_LOCATION") ?? "farm shed",
    ["host"] = Environment.MachineName,
    ["lang"] = ".NET",
});

var card = new AgentCard
{
    Name = "Sprayer",
    Description = "Green Valley farm agent sprayer",
    Version = "0.1.0",
    SupportedInterfaces = [new AgentInterface { Url = $"slim://{SelfName}", ProtocolBinding = "SLIMRPC", ProtocolVersion = "1.0" }],
    DefaultInputModes = ["text/plain"],
    DefaultOutputModes = ["text/plain"],
    Capabilities = new AgentCapabilities { Streaming = false, PushNotifications = false },
};
var a2a = new A2AServer(sprayer, new InMemoryTaskStore(), new ChannelEventNotifier(), NullLogger<A2AServer>.Instance);
await using var server = await slim.StartServerAsync(
    new SlimA2AServerOptions
    {
        Identity = SelfName,
        SharedSecret = secret,
        ResolveExtendedAgentCard = (_, _) => Task.FromResult(card),
    },
    a2a);

Console.WriteLine($"Sprayer up: {SelfName} via {endpoint} · location={sprayer.Location} host={Environment.MachineName} lang=.NET");

// Stop cleanly on Ctrl-C and on `docker stop` (SIGTERM), so open sessions are
// closed and callers switch over at once instead of waiting for a timeout.
var stop = new TaskCompletionSource();
using var onInt = PosixSignalRegistration.Create(PosixSignal.SIGINT, ctx => { ctx.Cancel = true; stop.TrySetResult(); });
using var onTerm = PosixSignalRegistration.Create(PosixSignal.SIGTERM, ctx => { ctx.Cancel = true; stop.TrySetResult(); });
await Task.WhenAny(stop.Task, server.Completion);

Console.WriteLine("Sprayer stopping...");
await server.StopAsync();
await caller.DisposeAsync();
return 0;

/// <summary>
/// Sends JSON requests to other agents by short id, keeping one client per
/// target and dropping it after a failure, like FarmAgent.call in farm/kit.py.
/// </summary>
sealed class Caller(SlimA2AConnection slim, string secret) : IAsyncDisposable
{
    // Must match AGENTS in farm/kit.py.
    static readonly Dictionary<string, string> Agents = new()
    {
        ["gh1"] = "greenvalley/farm/gh1",
        ["gh2"] = "greenvalley/farm/gh2",
        ["assistant"] = "greenvalley/farm/assistant",
    };

    readonly ConcurrentDictionary<string, SlimA2AClient> _clients = new();

    public async Task<JsonObject> CallAsync(string target, JsonObject request, TimeSpan timeout)
    {
        // SlimA2A wants a SLIM identity of its own for every client, so each
        // target gets one, e.g. greenvalley/farm/sprayer_gh1.
        var client = _clients.GetOrAdd(target, t => slim.CreateClient(new SlimA2AClientOptions
        {
            Identity = $"greenvalley/farm/sprayer_{t}",
            SharedSecret = secret,
            Remote = Agents[t],
        }));
        using var cts = new CancellationTokenSource(timeout);
        try
        {
            var response = await client.SendMessageAsync(new SendMessageRequest
            {
                Message = new Message
                {
                    Role = Role.User,
                    MessageId = Guid.NewGuid().ToString("N"),
                    Parts = [Part.FromText(request.ToJsonString())],
                },
            }, cts.Token);

            // Python and Go agents answer with a task holding an artifact.
            var text = response.Message?.Parts?.FirstOrDefault(p => p.Text is not null)?.Text
                ?? response.Task?.Artifacts?.SelectMany(a => a.Parts ?? []).FirstOrDefault(p => p.Text is not null)?.Text;
            return text is null ? [] : JsonNode.Parse(text)?.AsObject() ?? [];
        }
        catch
        {
            // The next call opens a fresh session to the name, which SLIM
            // routes to wherever that agent is now.
            if (_clients.TryRemove(target, out var dead))
                _ = dead.DisposeAsync().AsTask();
            throw;
        }
    }

    public async ValueTask DisposeAsync()
    {
        foreach (var client in _clients.Values)
            await client.DisposeAsync();
    }
}

namespace Sprayer
{
    sealed class SprayerAgent(Caller caller, JsonObject meta) : IAgentHandler
    {
        const int SpraySeconds = 8;

        readonly object _lock = new();
        readonly List<JsonObject> _jobs = [];
        DateTimeOffset _busyUntil = DateTimeOffset.MinValue;

        public string Location => meta["location"]!.GetValue<string>();

        public async Task ExecuteAsync(RequestContext context, AgentEventQueue eventQueue, CancellationToken cancellationToken)
        {
            JsonObject answer;
            try
            {
                var request = JsonNode.Parse(context.UserText ?? "")?.AsObject() ?? [];
                answer = await HandleAsync(request);
            }
            catch (Exception e)
            {
                // Report bad requests to the caller, keep serving.
                answer = new JsonObject { ["ok"] = false, ["error"] = e.Message };
            }
            var responder = new MessageResponder(eventQueue, context.ContextId);
            await responder.ReplyAsync(answer.ToJsonString(), cancellationToken: cancellationToken);
        }

        async Task<JsonObject> HandleAsync(JsonObject request)
        {
            switch (request["action"]?.GetValue<string>())
            {
                case "status":
                    lock (_lock)
                    {
                        var status = (JsonObject)meta.DeepClone();
                        status["kind"] = "sprayer";
                        status["busy_for"] = Math.Max(0, (int)Math.Round((_busyUntil - DateTimeOffset.UtcNow).TotalSeconds));
                        status["last_job"] = _jobs.Count > 0 ? _jobs[^1].DeepClone() : null;
                        status["jobs"] = _jobs.Count;
                        return status;
                    }

                case "spray":
                    var gh = request["greenhouse"]!.GetValue<string>();
                    var product = request["product"]?.GetValue<string>() ?? "neem oil";
                    var by = request["ordered_by"]?.GetValue<string>() ?? "unknown";

                    // One rig, one greenhouse at a time: book the next free slot.
                    TimeSpan wait;
                    lock (_lock)
                    {
                        var now = DateTimeOffset.UtcNow;
                        var start = _busyUntil > now ? _busyUntil : now;
                        _busyUntil = start.AddSeconds(SpraySeconds);
                        wait = start - now;
                    }
                    if (wait > TimeSpan.Zero)
                    {
                        Notify($"{gh.ToUpperInvariant()} queued, the rig moves there after the current job", gh);
                        _ = Task.Run(async () =>
                        {
                            await Task.Delay(wait);
                            try { await SprayAsync(gh, product, by); }
                            catch (Exception e) { Notify($"could not spray {gh.ToUpperInvariant()}: {e.Message}", gh); }
                        });
                        return new JsonObject { ["ok"] = true, ["say"] = $"{gh.ToUpperInvariant()} queued, starts in {Math.Ceiling(wait.TotalSeconds)} s" };
                    }

                    try
                    {
                        var reply = await SprayAsync(gh, product, by);
                        return new JsonObject { ["ok"] = true, ["say"] = $"spraying {gh.ToUpperInvariant()}: {reply["say"]}" };
                    }
                    catch
                    {
                        lock (_lock) _busyUntil = DateTimeOffset.UtcNow;  // free the slot again
                        throw;
                    }

                default:
                    throw new ArgumentException($"unknown action '{request["action"]}'");
            }
        }

        async Task<JsonObject> SprayAsync(string gh, string product, string by)
        {
            var reply = await caller.CallAsync(
                gh, new JsonObject { ["action"] = "spray", ["product"] = product }, TimeSpan.FromSeconds(3));
            lock (_lock)
            {
                _jobs.Add(new JsonObject
                {
                    ["greenhouse"] = gh,
                    ["product"] = product,
                    ["ordered_by"] = by,
                    ["at"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
                });
            }
            Notify($"spraying {gh.ToUpperInvariant()} with {product}", gh);
            return reply;
        }

        /// <summary>Tells the dashboard what just happened; never blocks, never fails.</summary>
        void Notify(string text, string? to) => _ = Task.Run(async () =>
        {
            try
            {
                await caller.CallAsync("assistant", new JsonObject
                {
                    ["action"] = "event",
                    ["from"] = "sprayer",
                    ["to"] = to,
                    ["text"] = text,
                    ["level"] = "info",
                }, TimeSpan.FromSeconds(1.5));
            }
            catch
            {
                // The dashboard may not be running; that's fine.
            }
        });
    }
}
