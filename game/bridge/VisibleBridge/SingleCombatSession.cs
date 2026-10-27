using System.Collections;
using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;

namespace Sts2.VisibleBridge;

internal static class SingleCombatSession
{
    private static readonly Dictionary<CardModel, string> CardIds = new(ReferenceEqualityComparer.Instance);
    private static CombatState? _combat;
    private static Player? _player;
    private static PlayerCombatState? _playerCombat;
    private static Creature[] _creatures = [];

    public static object? CurrentState { get; private set; }
    public static bool Terminated { get; private set; }

    public static void PreparePlayer(Player player)
    {
        foreach (RelicModel relic in player.Relics.ToArray())
            player.RemoveRelicInternal(relic, false);
        foreach (PotionModel? potion in player.PotionSlots.ToArray())
            if (potion is not null) player.DiscardPotionInternal(potion, false);
    }

    public static void Attach(Player player, CombatState combat)
    {
        if (_combat is not null)
            throw new InvalidOperationException("The visible test supports exactly one combat.");
        _player = player;
        _combat = combat;
        _playerCombat = player.PlayerCombatState ?? throw new InvalidOperationException("Player combat state is unavailable.");
        _creatures = combat.Creatures.ToArray();
        CardIds.Clear();
        for (int index = 0; index < player.Deck.Cards.Count; index++)
            CardIds[player.Deck.Cards[index]] = $"deck-{index}-{player.Deck.Cards[index].Id.Entry}";
    }

    public static (object State, List<LegalActionDto> Actions) Capture()
    {
        Player player = _player ?? throw new InvalidOperationException("The single combat has not started.");
        PlayerCombatState pcs = _playerCombat!;
        CombatState combat = _combat!;
        bool playerAlive = player.Creature.IsAlive;
        bool enemyAlive = combat.Enemies.Any(enemy => enemy.IsAlive);
        bool terminal = !playerAlive || !enemyAlive;
        List<LegalActionDto> actions = [];
        List<object> nativeActions = [];
        if (!terminal && pcs.Phase == PlayerTurnPhase.Play)
        {
            for (int index = 0; index < pcs.Hand.Cards.Count; index++)
            {
                CardModel card = pcs.Hand.Cards[index];
                if (!card.CanPlay()) continue;
                if (card.TargetType.IsSingleTarget() && card.TargetType != TargetType.Self)
                {
                    foreach (Creature target in combat.Creatures.Where(card.IsValidTarget))
                    {
                        string actionId = $"play_card:{index}:target:{target.CombatId}";
                        actions.Add(new LegalActionDto { ActionId = actionId, ActionType = "play_card" });
                        nativeActions.Add(Action(actionId, "play_card", new Dictionary<string, object?>
                        {
                            ["instance_id"] = CardId(card), ["target_id"] = target.CombatId
                        }));
                    }
                }
                else
                {
                    string actionId = $"play_card:{index}";
                    actions.Add(new LegalActionDto { ActionId = actionId, ActionType = "play_card" });
                    nativeActions.Add(Action(actionId, "play_card", new Dictionary<string, object?>
                    {
                        ["instance_id"] = CardId(card), ["target_id"] = null
                    }));
                }
            }
            actions.Add(new LegalActionDto { ActionId = "end_turn", ActionType = "end_turn" });
            nativeActions.Add(Action("end_turn", "end_turn", new Dictionary<string, object?>()));
        }

        object state = new
        {
            terminated = terminal,
            victory = playerAlive && !enemyAlive,
            observation = new
            {
                combat = new
                {
                    turn = pcs.TurnNumber, phase = pcs.Phase.ToString(),
                    energy = pcs.Energy, max_energy = pcs.MaxEnergy, stars = pcs.Stars,
                    creatures = _creatures.Select(CreatureSnapshot).ToArray(),
                    piles = new[] { pcs.Hand, pcs.DrawPile, pcs.DiscardPile, pcs.ExhaustPile, pcs.PlayPile }
                        .Select(PileSnapshot).ToArray(),
                    orbs = new
                    {
                        capacity = pcs.OrbQueue.Capacity,
                        entries = pcs.OrbQueue.Orbs.Select(orb => new
                        {
                            model_id = orb.Id.Entry, passive = orb.PassiveVal, evoke = orb.EvokeVal
                        }).ToArray()
                    }
                },
                inventory = new
                {
                    relics = player.Relics.Select(relic => new { model_id = relic.Id.Entry }).ToArray(),
                    potions = player.PotionSlots.Select((potion, slot) => potion is null
                        ? null : new { slot, model_id = potion.Id.Entry }).ToArray()
                },
                outstanding_choice = (object?)null
            },
            legal_actions = nativeActions.ToArray()
        };
        CurrentState = state;
        Terminated = terminal;
        return (state, actions);
    }

    public static void CaptureBeforeCombatCleanup()
    {
        if (!FullAppBridgeServer.SingleCombatMode || _combat is null || Terminated) return;
        Capture();
        if (!Terminated)
            throw new InvalidOperationException("Combat ended without a native victory or player death.");
        FullAppBridgeServer.PublishSingleCombatTerminal();
    }

    private static object PileSnapshot(CardPile pile) => new
    {
        name = pile.Type switch
        {
            PileType.Hand => "Hand", PileType.Draw => "DrawPile", PileType.Discard => "DiscardPile",
            PileType.Exhaust => "ExhaustPile", PileType.Play => "PlayPile", _ => pile.Type.ToString()
        },
        cards = pile.Cards.Select(card => new
        {
            instance_id = CardId(card), model_id = card.Id.Entry,
            energy_cost = card.EnergyCost.GetResolved(), costs_x = card.EnergyCost.CostsX,
            upgrades = card.CurrentUpgradeLevel,
            enchantment = card.Enchantment is null ? null : new
            {
                model_id = card.Enchantment.Id.Entry, amount = card.Enchantment.Amount
            },
            native_state = SavedState(card)
        }).ToArray()
    };

    private static object CreatureSnapshot(Creature creature)
    {
        object? move = creature.Monster?.NextMove;
        object[] intents = move is null ? [] : Enumerate(GetMember(move, "Intents"))
            .Select(intent => IntentSnapshot(intent, creature)).ToArray();
        return new
        {
            combat_id = creature.CombatId, model_id = creature.ModelId.Entry, side = creature.Side.ToString(),
            hp = creature.CurrentHp, max_hp = creature.MaxHp, block = creature.Block, alive = creature.IsAlive,
            powers = creature.Powers.Select(power => new { model_id = power.Id.Entry, amount = power.Amount }).ToArray(),
            next_move = move is null ? null : new { id = GetMember(move, "Id"), intents }
        };
    }

    private static object IntentSnapshot(object intent, Creature owner)
    {
        object? damage = null;
        object? repeats = null;
        for (Type? type = intent.GetType(); type is not null; type = type.BaseType)
        {
            if (type.FullName != "MegaCrit.Sts2.Core.MonsterMoves.Intents.AttackIntent") continue;
            damage = Invoke(intent, "GetSingleDamage", _combat!.Allies, owner);
            repeats = GetMember(intent, "Repeats");
            break;
        }
        return new { intent_type = GetMember(intent, "IntentType")?.ToString(), damage, repeats };
    }

    private static string CardId(CardModel card)
    {
        if (CardIds.TryGetValue(card, out string? id)) return id;
        if (card.DeckVersion is not null && CardIds.TryGetValue(card.DeckVersion, out id))
        {
            CardIds[card] = id;
            return id;
        }
        id = $"combat-{CardIds.Count}-{card.Id.Entry}";
        CardIds[card] = id;
        return id;
    }

    private static object Action(string actionId, string kind, Dictionary<string, object?> parameters) =>
        new { action_id = actionId, kind, parameters };

    private static SortedDictionary<string, object?> SavedState(CardModel card)
    {
        object? props = card.ToSerializable().Props;
        SortedDictionary<string, object?> result = new(StringComparer.Ordinal);
        if (props is null) return result;
        foreach (string fieldName in new[] { "ints", "bools", "strings", "intArrays" })
        {
            foreach (object item in Enumerate(props.GetType().GetField(fieldName)?.GetValue(props)))
            {
                Type type = item.GetType();
                result[Convert.ToString(type.GetField("name")!.GetValue(item))!] = type.GetField("value")!.GetValue(item);
            }
        }
        foreach (string fieldName in new[] { "modelIds", "cards", "cardArrays" })
            if (Enumerate(props.GetType().GetField(fieldName)?.GetValue(props)).Any())
                throw new NotSupportedException($"Unsupported saved card properties: {fieldName}.");
        return result;
    }

    private static IEnumerable<object> Enumerate(object? value) => value is IEnumerable sequence ? sequence.Cast<object>() : [];

    private static object? GetMember(object target, string name) =>
        target.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(target)
        ?? target.GetType().GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(target);

    private static object? Invoke(object target, string name, params object?[] arguments)
    {
        MethodInfo method = target.GetType().GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
            .Where(candidate => candidate.Name == name && candidate.GetParameters().Length == arguments.Length)
            .Single(candidate => candidate.GetParameters().Select(parameter => parameter.ParameterType)
                .Zip(arguments, (type, argument) => argument is null || type.IsInstanceOfType(argument)).All(match => match));
        return method.Invoke(target, arguments);
    }
}
