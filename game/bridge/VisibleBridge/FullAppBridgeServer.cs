using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.Nodes;

namespace Sts2.VisibleBridge;

public static class FullAppBridgeServer
{
    private static TcpListener? _listener;
    private static TcpClient? _client;
    private static NetworkStream? _stream;
    private static StreamWriter? _writer;
    private static StreamReader? _reader;
    private static TaskCompletionSource<string>? _pendingActionTcs;
    private static TaskCompletionSource<bool>? _initialBoundaryTcs;
    private static readonly object SyncLock = new();

    public static int BoundPort { get; private set; }
    public static ObservationDto? CurrentObservation { get; set; }
    public static List<LegalActionDto> CurrentLegalActions { get; set; } = new();
    public static List<string> ActionHistory { get; } = new();
    public static List<string> StateHashHistory { get; } = new();

    public static string RequestedSeed { get; private set; } = "A1B2C3D4E5";
    public static string RequestedCharacter { get; private set; } = "IRONCLAD";
    public static int RequestedAscension { get; private set; } = 0;
    public static bool IsRunStarted { get; private set; }
    public static bool SingleCombatMode { get; private set; }

    public static void Start(int preferredPort, string portFilePath)
    {
        string forceChar = System.Environment.GetEnvironmentVariable("STS2_FORCE_CHARACTER") ?? "";
        if (!string.IsNullOrWhiteSpace(forceChar))
        {
            RequestedCharacter = forceChar;
        }

        _listener = new TcpListener(IPAddress.Loopback, preferredPort);
        _listener.Start();
        BoundPort = ((IPEndPoint)_listener.LocalEndpoint).Port;

        if (!string.IsNullOrWhiteSpace(portFilePath))
        {
            Directory.CreateDirectory(Path.GetDirectoryName(portFilePath)!);
            File.WriteAllText(portFilePath, BoundPort.ToString());
        }

        Task.Run(ListenLoopAsync);
    }

    private static async Task ListenLoopAsync()
    {
        while (_listener is not null)
        {
            try
            {
                TcpClient client = await _listener.AcceptTcpClientAsync();
                lock (SyncLock)
                {
                    _client?.Dispose();
                    _client = client;
                    _stream = client.GetStream();
                    _writer = new StreamWriter(_stream, new UTF8Encoding(false)) { AutoFlush = true };
                    _reader = new StreamReader(_stream, new UTF8Encoding(false));
                }

                _ = HandleClientAsync(_reader, _writer);
            }
            catch
            {
                // listener closed or error
                break;
            }
        }
    }

    private static async Task HandleClientAsync(StreamReader reader, StreamWriter writer)
    {
        while (true)
        {
            string? line = await reader.ReadLineAsync();
            if (line is null) break;
            if (string.IsNullOrWhiteSpace(line)) continue;

            RpcRequest? request = null;
            try
            {
                request = JsonSerializer.Deserialize<RpcRequest>(line);
                if (request is null) continue;

                object? result = await DispatchRequestAsync(request.Method, request.Params);
                var response = new RpcResponse
                {
                    Id = request.Id,
                    Result = result,
                };
                string responseJson = JsonSerializer.Serialize(response);
                await writer.WriteLineAsync(responseJson);
                if (request.Method.Equals("close", StringComparison.OrdinalIgnoreCase))
                {
                    Callable.From(() => ((SceneTree)Engine.GetMainLoop()).Quit(0)).CallDeferred();
                    return;
                }
            }
            catch (Exception ex)
            {
                var errorResponse = new RpcResponse
                {
                    Id = request?.Id ?? 0,
                    Error = ex.Message,
                };
                await writer.WriteLineAsync(JsonSerializer.Serialize(errorResponse));
            }
        }
    }

    private static async Task<object?> DispatchRequestAsync(string method, Dictionary<string, object?>? parameters)
    {
        switch (method.ToLowerInvariant())
        {
            case "hello":
                return new Dictionary<string, object?>
                {
                    ["status"] = "ready",
                    ["bridge_id"] = "sts2-visible-bridge",
                    ["visible"] = true,
                    ["single_combat"] = true,
                    ["presentation_suppressed"] = false,
                    ["display_server"] = FullAppBridgeMod.DisplayName,
                    ["game_assembly_version"] = typeof(NGame).Assembly.GetName().Version?.ToString(),
                    ["game_assembly_path"] = typeof(NGame).Assembly.Location,
                    ["pid"] = System.Environment.ProcessId,
                    ["bound_port"] = BoundPort,
                };

            case "reset_combat":
            case "start_run":
                string seed = parameters?.GetValueOrDefault("seed")?.ToString() ?? RequestedSeed;
                string character = parameters?.GetValueOrDefault("character")?.ToString() ?? RequestedCharacter;
                string? ascensionText = parameters?.GetValueOrDefault("ascension")?.ToString();
                int ascension = 0;
                if (ascensionText is not null && !int.TryParse(ascensionText, out ascension))
                    throw new ArgumentException("ascension must be the integer 0.");
                if (ascension != 0)
                    throw new NotSupportedException("VisibleBridge currently supports ascension 0 only.");

                Task initialBoundary;
                lock (SyncLock)
                {
                    if (IsRunStarted)
                        throw new InvalidOperationException("This process already started a run; create a fresh isolated process.");
                    RequestedSeed = seed;
                    RequestedCharacter = character;
                    RequestedAscension = ascension;
                    SingleCombatMode = method.Equals("reset_combat", StringComparison.OrdinalIgnoreCase);
                    if (SingleCombatMode && !character.Equals("IRONCLAD", StringComparison.OrdinalIgnoreCase))
                        throw new NotSupportedException("The single-combat Actor supports Ironclad only.");
                    _initialBoundaryTcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    initialBoundary = _initialBoundaryTcs.Task;
                    IsRunStarted = true;
                }
                await initialBoundary;

                if (SingleCombatMode) return SingleCombatSession.CurrentState;

                return new Dictionary<string, object?>
                {
                    ["started"] = true,
                    ["seed"] = RequestedSeed,
                    ["character"] = RequestedCharacter,
                    ["ascension"] = RequestedAscension,
                    ["observation"] = CurrentObservation,
                    ["legal_actions"] = CurrentLegalActions,
                };

            case "observe":
                return CurrentObservation;

            case "observe_combat":
                if (!SingleCombatMode) throw new InvalidOperationException("Call reset_combat first.");
                return SingleCombatSession.CurrentState;

            case "legal_actions":
                return CurrentLegalActions;

            case "step_combat":
            case "step":
                string actionId = parameters?.GetValueOrDefault("action_id")?.ToString() ?? "";
                if (string.IsNullOrWhiteSpace(actionId))
                    throw new ArgumentException("step requires action_id.");

                Task nextBoundary;
                lock (SyncLock)
                {
                    if (method.Equals("step_combat", StringComparison.OrdinalIgnoreCase) != SingleCombatMode)
                        throw new InvalidOperationException("Use step_combat after reset_combat, or step after start_run.");
                    if (!IsRunStarted || CurrentObservation is null || CurrentObservation.IsTerminal)
                        throw new InvalidOperationException("No active nonterminal decision is available.");
                    if (_pendingActionTcs is null || _pendingActionTcs.Task.IsCompleted)
                        throw new InvalidOperationException("The game is not waiting for an action.");
                    if (!CurrentLegalActions.Any(action => StringComparer.Ordinal.Equals(action.ActionId, actionId)))
                        throw new ArgumentException("action_id is not legal at the current decision boundary.");

                    _initialBoundaryTcs = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    nextBoundary = _initialBoundaryTcs.Task;
                    ActionHistory.Add(actionId);
                    _pendingActionTcs.SetResult(actionId);
                }
                await nextBoundary;

                if (SingleCombatMode) return SingleCombatSession.CurrentState;

                if (CurrentObservation is not null)
                {
                    StateHashHistory.Add(CurrentObservation.StateHash);
                }

                return new Dictionary<string, object?>
                {
                    ["observation"] = CurrentObservation,
                    ["legal_actions"] = CurrentLegalActions,
                };

            case "history":
                return new Dictionary<string, object?>
                {
                    ["seed"] = RequestedSeed,
                    ["character"] = RequestedCharacter,
                    ["ascension"] = RequestedAscension,
                    ["actions"] = ActionHistory,
                    ["state_hashes"] = StateHashHistory,
                };

            case "close":
                return new Dictionary<string, object?> { ["closed"] = true };

            default:
                throw new NotSupportedException($"Unknown RPC method: {method}");
        }
    }

    public static async Task<string> WaitForCoordinatorActionAsync(
        string phase,
        bool isTerminal,
        bool isVictory,
        object? contextObject = null)
    {
        FullAppBridgeMod.EnsureInteractivePresentation($"boundary:{phase}");
        AutoSlayer.CurrentWatchdog?.Reset($"Bridge:{phase}");
        var (obs, actions) = FullAppStateTracker.CreateStateSnapshot(phase, isTerminal, isVictory, contextObject);
        if (SingleCombatMode)
        {
            if (phase != "combat")
                throw new InvalidOperationException($"Unexpected single-combat decision: {phase}.");
            var (_, combatActions) = SingleCombatSession.Capture();
            actions = combatActions;
        }
        Task<string>? actionTask;
        lock (SyncLock)
        {
            CurrentObservation = obs;
            CurrentLegalActions = actions;
            if (StateHashHistory.Count == 0)
                StateHashHistory.Add(obs.StateHash);

            // Publish readiness only after the action receiver exists.
            _pendingActionTcs = isTerminal ? null :
                new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
            actionTask = _pendingActionTcs?.Task;
            _initialBoundaryTcs?.TrySetResult(true);
        }

        if (isTerminal)
            return "terminal_halt";

        // A paused visible demo waits for the coordinator without selecting an action.
        while (!actionTask!.IsCompleted)
        {
            await Task.WhenAny(actionTask, Task.Delay(1000));
            AutoSlayer.CurrentWatchdog?.Reset($"Bridge:{phase}:waiting-for-coordinator");
        }
        string chosenAction = await actionTask;
        AutoSlayer.CurrentWatchdog?.Reset($"Bridge:{phase}:{chosenAction}");
        return chosenAction;
    }

    public static void PublishSingleCombatTerminal()
    {
        lock (SyncLock)
        {
            if (CurrentObservation is not null) CurrentObservation.IsTerminal = true;
            CurrentLegalActions = [];
            _pendingActionTcs = null;
            _initialBoundaryTcs?.TrySetResult(true);
        }
    }

    public static void PublishFailure(Exception error)
    {
        lock (SyncLock)
        {
            _initialBoundaryTcs?.TrySetException(error);
        }
    }
}
