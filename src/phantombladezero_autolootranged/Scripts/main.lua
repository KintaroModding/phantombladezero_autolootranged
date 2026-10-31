-- Auto Loot (Ranged) for Phantom Blade Zero.
--
-- Loot piles near the player (BP_Rubbish and its children, which also hold notes and pages) are picked up by
-- themselves from a short distance (Range), without walking into the game's own interact range. Chests open by
-- themselves only when the game's interact prompt targets them: their opening montage moves the player to the chest
-- (motion warping), so a chest opened from a distance would pull the player over. Both use the game's own interact
-- call, PBZInteractComponent:DoInteract, the call a key press makes: the pickup, the chest's opening, the bag, the save
-- flag and the popup run as in the game. A port of the earlier JavaScript version of the mod, which ran inside a
-- replaced game script and took only the prompt's target.
--
-- Left for the player (a key press as in the game): piles with a weapon, medicine or accessory (the game's equip or
-- replace popup; chests with gear open); story and quest items (narrative item level or category, memory flowers and
-- petals, items that set a tag when owned); scripted loot (its own flow, a level sequence other than the stock chest
-- sequence, a story tag set on success); locked chests (they spend a key item). Mode Materials or Common: only piles of
-- materials, no chests. Nothing is taken in combat, in the air, during an action or a status effect, when dead, while
-- the game is paused, in dialogue, during another interaction or a sequence, or while the pickup popup is open. A
-- target is tried at most once in RETRY_SECONDS, a chest once per game start.
--
-- How: every CHECK_INTERVAL_MS on the game thread. The loot piles are listed on every LIST_EVERY-th check
-- (GameplayStatics:GetAllActorsOfClass; piles do not move, so their locations are kept with them), and each check
-- measures the player's distance to them in Lua. A kept pile is used only after its address and full name match
-- again (UE4SS's IsValid does not tell a reused address). The prompt's target is the component's GetInteractActor.
-- Only the classes and engine objects are searched by name: at start, after a map load and while one is missing. After
-- STOP_AFTER_ERRORS errors the mod turns itself off until the next game start, and the game plays as normal.
--
-- Settings: the player edits Scripts\config.lua. The mod reads it at start and again every CONFIG_INTERVAL_MS, so a
-- change shows without a restart; a broken or half-saved file keeps the last settings. DEFAULTS holds every setting
-- with its default. A player (or a mod manager) keeps their config.lua across updates, so a new main.lua can meet an
-- older file: a setting that the file lacks, or has with another type, keeps its default, and the log says so. Read a
-- setting only from Settings, never as `config.X or default`: for a true/false setting, `false or true` is true.

local UEHelpers = require("UEHelpers")

local MOD_NAME = "Auto Loot (Ranged)"
local VERSION = "1.2"
local CHECK_INTERVAL_MS = 250
local CONFIG_INTERVAL_MS = 5000
-- List the loot piles again on every n-th check (2 s).
local LIST_EVERY = 8
-- While a class or engine object is missing, search for it on every n-th check only (10 s).
local SEARCH_EVERY = 40
local RETRY_SECONDS = 10
local STOP_AFTER_ERRORS = 3
-- Log at most this many error lines in total.
local MAX_ERROR_LOGS = 5
-- LogDetail: at most this many lines per game start, and a status line on every n-th check (30 s).
local MAX_DETAIL_LOGS = 400
local STATUS_EVERY = 120

-- Every setting with its default; config.lua ships the same names and values (tests\test_main.py checks it).
-- Range: the game's own pile range is its trigger box, 2.5 m straight on and up to 3.5 m across the corners; 4 m comes
-- always a little before the game's prompt (measured and chosen in game, 2026-10-31).
local DEFAULTS = {
    Enable = true,
    Range = 4,
    Chests = true,
    Mode = "All",
    LogDetail = false,
}
-- Number settings outside their range are clamped; text settings must be one of their choices (any case).
local LIMITS = {
    Range = { 0.5, 10 },
}
local CHOICES = {
    Mode = { "All", "Materials", "Common" },
}

local PILE_CLASS = "/Game/PBZContent/Blueprint/Item/Device/BP_Rubbish.BP_Rubbish_C"
local CHEST_CLASS = "/Game/PBZContent/Blueprint/Item/Device/BP_Chest.BP_Chest_C"
local CHEST_SEQUENCE = "/Game/PBZContent/Sequencer/Switch/LS_Interact_Chest.LS_Interact_Chest"
local INTERACT_DEFAULT = "/Script/ProjectPBZ.Default__PBZInteractComponent"
local BACKPACK_CLASS = "/Script/ProjectPBZ.PBZBackPackSubsystem"
local BACKPACK_DEFAULT = "/Script/ProjectPBZ.Default__PBZBackPackSubsystem"
local EVENTS_CLASS = "/Script/ProjectPBZ.EventPipelineManager"
local SUBSYSTEMS_DEFAULT = "/Script/Engine.Default__SubsystemBlueprintLibrary"
-- The player's states that stop the mod (a parent tag also counts its children).
local BLOCK_TAGS = { "CharacterState.InCombat", "CharacterState.Dead", "CharacterState.Space.Air",
    "CharacterState.StatusInfliction", "CharacterState.Action" }
-- Item enums (ProjectPBZ_enums.hpp): EItemLevel Narrative; EItemDetailType Material, NarrativeItem; EItemTypes
-- MemoryFlower, MemoryFlowerPetal. Mode Common: the levels whose pile glows white (C, D, E, F) or pale blue (B, B1-B3).
local LEVEL_NARRATIVE = 4
local DETAIL_MATERIAL = 1
local DETAIL_NARRATIVE = 2
local TYPE_MEMORY_FLOWER = 4
local TYPE_MEMORY_FLOWER_PETAL = 5
local COMMON_LEVELS = { [0] = true, [5] = true, [6] = true, [7] = true, [1] = true, [12] = true, [13] = true,
    [14] = true }

local BLOCK_FNAMES = {}
for i, tag in ipairs(BLOCK_TAGS) do
    BLOCK_FNAMES[i] = FName(tag, EFindName.FNAME_Add)
end

local errors = 0
local details = 0

local function Log(message)
    print(string.format("[%s] %s\n", MOD_NAME, message))
end

local function LogError(message)
    errors = errors + 1
    if errors <= MAX_ERROR_LOGS then
        Log(message)
    end
end

local Settings = {}

-- LogDetail lines; message is a function, so nothing is built while LogDetail is off.
local function Detail(message)
    if Settings.LogDetail and details < MAX_DETAIL_LOGS then
        details = details + 1
        Log(message())
    end
end

local function Valid(object)
    if object == nil then
        return false
    end
    local ok, valid = pcall(function()
        return object:IsValid()
    end)
    return ok and valid == true
end

-- The object's path without its class ("BP_Rubbish_C /Game/Maps/...BP_Rubbish_C_1" -> "/Game/Maps/...").
local function PathOf(object)
    local full = object:GetFullName()
    return full:match("^%S+%s+(.+)$") or full
end

-- The last part of a path, for the log.
local function Short(path)
    return path:match("[^.:/]+$") or path
end

-- An item that UE4SS gives as a RemoteUnrealParam (an array out parameter, a TMap key): :get() is the value; anything
-- else stays as it is.
local function Unwrap(item)
    if type(item) ~= "userdata" and type(item) ~= "table" then
        return item
    end
    local ok, value = pcall(function()
        return item:get()
    end)
    if ok and value ~= nil then
        return value
    end
    return item
end

---------------------------------------------------------------------------------------------------------------------
-- Settings
---------------------------------------------------------------------------------------------------------------------

local function CopyDefaults()
    local settings = {}
    for name, value in pairs(DEFAULTS) do
        settings[name] = value
    end
    return settings
end

-- One line for the log: every setting, sorted by name.
local function Describe(settings)
    local names = {}
    for name in pairs(settings) do
        names[#names + 1] = name
    end
    table.sort(names)
    local parts = {}
    for i, name in ipairs(names) do
        parts[i] = name .. "=" .. tostring(settings[name])
    end
    return table.concat(parts, ", ")
end

-- Lua's line number and reason, without the chunk name.
local function Reason(message)
    message = tostring(message)
    local line, reason = message:match("config%.lua:(%d+): ([^\n]*)")
    return line and ("line " .. line .. ": " .. reason) or (message:gsub("%s*\n%s*", " "))
end

-- The choice with this name in any case, or nil.
local function Choice(name, value)
    for _, choice in ipairs(CHOICES[name]) do
        if choice:lower() == value:lower() then
            return choice
        end
    end
    return nil
end

-- DEFAULTS with the values of config.lua's text over them; nil and the reason when the text is not a usable file.
local function ParseConfig(text)
    local chunk, problem = load(text, "=config.lua", "t", {})
    if not chunk then
        return nil, "config.lua not read (" .. Reason(problem) .. ")"
    end
    local ok, config = pcall(chunk)
    if not ok then
        return nil, "config.lua not read (" .. Reason(config) .. ")"
    end
    if type(config) ~= "table" then
        return nil, "config.lua does not return a table"
    end
    local settings = CopyDefaults()
    for name, value in pairs(config) do
        local default = DEFAULTS[name]
        local limit = LIMITS[name]
        if default == nil then
            Log("config.lua: unknown setting " .. tostring(name) .. " ignored.")
        elseif type(value) ~= type(default) then
            Log(string.format("config.lua: %s must be a %s; the default %s is used.", name, type(default),
                tostring(default)))
        elseif limit and value ~= value then
            Log(string.format("config.lua: %s is not a number; the default %s is used.", name, tostring(default)))
        elseif limit and (value < limit[1] or value > limit[2]) then
            local clamped = math.min(limit[2], math.max(limit[1], value))
            Log(string.format("config.lua: %s = %s is outside %s to %s; %s is used.", name, tostring(value),
                tostring(limit[1]), tostring(limit[2]), tostring(clamped)))
            settings[name] = clamped
        elseif CHOICES[name] and not Choice(name, value) then
            Log(string.format("config.lua: %s = %q is not one of %s; the default %s is used.", name, value,
                table.concat(CHOICES[name], ", "), tostring(default)))
        elseif CHOICES[name] then
            settings[name] = Choice(name, value)
        else
            settings[name] = value
        end
    end
    return settings
end

local configPath = nil
-- The text of the last read; false: no file at the last read; nil: not read yet.
local configText = nil

-- Reads config.lua when its text changed since the last read. At start, a missing or broken file means the defaults;
-- later, a broken file keeps the last settings (a half-saved file), and a missing one means the defaults.
local function ReadConfig()
    local atStart = configText == nil
    configPath = configPath or package.searchpath("config", package.path)
    local text = false
    if configPath then
        local file = io.open(configPath, "rb")
        if file then
            text = file:read("a") or false
            file:close()
        end
    end
    if text == configText then
        return
    end
    configText = text
    local settings, problem
    if text == false then
        settings, problem = CopyDefaults(), nil
        Log("No config.lua; every setting uses its default.")
    else
        settings, problem = ParseConfig(text)
    end
    if not settings then
        if atStart then
            Log(problem .. "; every setting uses its default.")
            settings = CopyDefaults()
        else
            Log(problem .. "; the last settings stay.")
            return
        end
    end
    local line = Describe(settings)
    if atStart or line ~= Describe(Settings) then
        Log("Settings: " .. line)
    end
    Settings = settings
end

---------------------------------------------------------------------------------------------------------------------
-- Game objects
---------------------------------------------------------------------------------------------------------------------

local found = {}            -- name -> object found by StaticFindObject (classes and default objects)
local controller = nil
local interact, interactOwner = nil, nil
local backpack, events = nil, nil
local piles = {}            -- the listed piles: { actor, address, name, x, y, z }
local tried = {}            -- target full name -> os.clock() of the last try
local opened = {}           -- chest full name -> true: opened by the mod in this game start
local checks = 0
local searchDue = true
local listDue = true
local stopped = false
local checkErrors = 0
local lastPrompt = nil      -- LogDetail: the last prompt target, to log its distance when it changes
local counts = { picked = 0, opened = 0, skipped = 0 }

local SEARCHED = {
    pileClass = PILE_CLASS,
    chestClass = CHEST_CLASS,
    interactDefault = INTERACT_DEFAULT,
    backpackClass = BACKPACK_CLASS,
    backpackDefault = BACKPACK_DEFAULT,
    eventsClass = EVENTS_CLASS,
    subsystems = SUBSYSTEMS_DEFAULT,
}

-- The classes and default objects: at start, after a map load, and while one is missing on every SEARCH_EVERY-th
-- check (a search by name costs a few milliseconds).
local function FindObjects()
    local missing = false
    for key in pairs(SEARCHED) do
        if not Valid(found[key]) then
            missing = true
        end
    end
    if not missing or (not searchDue and checks % SEARCH_EVERY ~= 0) then
        return
    end
    searchDue = false
    local names = {}
    for key, path in pairs(SEARCHED) do
        if not Valid(found[key]) then
            found[key] = StaticFindObject(path)
            if not Valid(found[key]) then
                names[#names + 1] = Short(path)
            end
        end
    end
    table.sort(names)
    Detail(function() return #names == 0 and "classes: all found" or ("classes: not loaded yet: "
        .. table.concat(names, ", ")) end)
end

-- The piles near the player, with their locations (piles do not move).
local function ListPiles(context)
    local out = {}
    UEHelpers.GetGameplayStatics():GetAllActorsOfClass(context, found.pileClass, out)
    local list = {}
    for _, item in ipairs(out) do
        local actor = Unwrap(item)
        if Valid(actor) then
            local where = actor:K2_GetActorLocation()
            list[#list + 1] = { actor = actor, address = actor:GetAddress(), name = actor:GetFullName(), x = where.X,
                y = where.Y, z = where.Z }
        end
    end
    if #list ~= #piles then
        Detail(function() return "listed BP_Rubbish: " .. #list .. " piles" end)
    end
    piles = list
end

-- The kept actor is still the one listed: same address, same full name.
local function Same(entry)
    return Valid(entry.actor) and entry.actor:GetAddress() == entry.address and entry.actor:GetFullName() == entry.name
end

local function Recent(path, now)
    local at = tried[path]
    return at ~= nil and now - at < RETRY_SECONDS
end

---------------------------------------------------------------------------------------------------------------------
-- Checks
---------------------------------------------------------------------------------------------------------------------

-- Nothing in the way of an interaction: nil when ready, else the reason.
local function Blocked(player)
    if UEHelpers.GetGameplayStatics():IsGamePaused(controller) then
        return "the game is paused"
    end
    if Valid(interact:GetInteractingActor()) then
        return "another interaction runs"
    end
    if interact:HasValidLevelSequencePlayer() then
        return "a sequence plays"
    end
    if not Valid(backpack) then
        backpack = found.subsystems:GetWorldSubsystem(controller, found.backpackClass)
    end
    if not Valid(backpack) or backpack:IsActivatePickupWidget() or backpack:IsDisplayPickupUI() then
        return "the pickup popup is open"
    end
    if not Valid(events) then
        events = found.subsystems:GetWorldSubsystem(controller, found.eventsClass)
    end
    if not Valid(events) or Valid(events.DialogueWidget) then
        return "a dialogue runs"
    end
    local abilities = player.AbilitySystemComponent
    if not Valid(abilities) then
        return "no ability system"
    end
    for i, name in ipairs(BLOCK_FNAMES) do
        if abilities:GetGameplayTagCount({ TagName = name }) > 0 then
            return BLOCK_TAGS[i]
        end
    end
    return nil
end

local function Count(map)
    local n = 0
    map:ForEach(function()
        n = n + 1
    end)
    return n
end

local function TagCount(container)
    return #container.GameplayTags
end

-- Story and quest items stay with the player: the narrative level or category, memory flowers and petals, items that
-- set a tag when owned. Plain notes and book pages pass. nil when all items may be taken, else the reason.
local function LootReason(map)
    local reason = nil
    map:ForEach(function(key)
        local id = Unwrap(key)
        local data = found.backpackDefault:GetPropData(id)
        if data.ItemLevel == LEVEL_NARRATIVE or data.ItemDetailType == DETAIL_NARRATIVE
            or data.ItemType == TYPE_MEMORY_FLOWER or data.ItemType == TYPE_MEMORY_FLOWER_PETAL then
            reason = "story item " .. id:ToString()
        elseif data.AddTagWhenHas.TagName:ToString() ~= "None" then
            reason = "quest item " .. id:ToString()
        elseif Settings.Mode ~= "All" and data.ItemDetailType ~= DETAIL_MATERIAL then
            reason = "not a material: " .. id:ToString()
        elseif Settings.Mode == "Common" and not COMMON_LEVELS[data.ItemLevel] then
            -- One purple or orange item keeps the whole pile: the pickup takes all of it.
            reason = "a rare material: " .. id:ToString()
        end
        -- true stops the loop at the first item that stays.
        return reason ~= nil
    end)
    return reason
end

local function PileReason(pile)
    if pile["Not Interact"] or pile.bDestroyed then
        return "already taken"
    end
    local flow = pile.BP_PBZFlowComponent
    if not Valid(flow) or Valid(flow.RootFlow) or Valid(pile.LevelSequenceAsset) then
        return "scripted (a flow or a sequence)"
    end
    if TagCount(pile.AddTagWhenSuccess) > 0 then
        return "sets a story tag"
    end
    -- The pile's own loot list; its Pickup clears and builds it the same way.
    pile["Collect Items"](pile)
    if #pile.ReallyGetWeapons + #pile.ReallyMedicines + #pile.ReallyAccessories > 0 then
        return "a weapon, medicine or accessory (the game's equip choice)"
    end
    return LootReason(pile.PickupMap)
end

local function ChestReason(chest)
    if chest.CurSwitchState then
        return "already open"
    end
    local flow = chest.BP_PBZFlowComponent
    local sequence = chest.LevelSequenceAsset
    if not Valid(flow) or Valid(flow.RootFlow) or (Valid(sequence) and PathOf(sequence) ~= CHEST_SEQUENCE) then
        return "scripted (a flow or its own sequence)"
    end
    if Count(chest.RequiredItems) > 0 then
        return "locked (needs a key item)"
    end
    if TagCount(chest.AddTagWhenSuccess) > 0 or TagCount(chest.Tags_CompletedInteract) > 0 then
        return "sets a story tag"
    end
    -- The chest's own loot list; gear stays allowed: its equip popup is the stock one at the chest.
    chest["Collect Items"](chest)
    return LootReason(chest.PickupMaps)
end

-- The check: the prompt's chest, else the nearest pile within Range; then the game's state and the target's loot.
local function Check()
    if not Settings.Enable then
        return
    end
    FindObjects()
    if not Valid(found.pileClass) or not Valid(found.interactDefault) then
        return
    end
    if not Valid(controller) then
        controller = UEHelpers.GetPlayerController()
        if not Valid(controller) then
            return
        end
    end
    local player = controller.Pawn
    if not Valid(player) then
        return
    end
    local owner = player:GetAddress()
    if not Valid(interact) or interactOwner ~= owner then
        interact = found.interactDefault:GetInteractComponent(player)
        interactOwner = owner
        if not Valid(interact) then
            return
        end
    end
    if listDue or checks % LIST_EVERY == 0 then
        listDue = false
        ListPiles(controller)
    end

    local here = player:K2_GetActorLocation()
    local function Distance(x, y, z)
        return math.sqrt((x - here.X) ^ 2 + (y - here.Y) ^ 2 + (z - here.Z) ^ 2) / 100
    end
    local now = os.clock()

    -- key: the target's full name, for the retry time and the opened chests.
    local target, key, chest, distance = nil, nil, false, nil
    local prompt = interact:GetInteractActor()
    if Valid(prompt) then
        if Settings.LogDetail and prompt:GetAddress() ~= lastPrompt then
            -- The game's own interact range, measured: the distance at which its prompt takes a target.
            lastPrompt = prompt:GetAddress()
            local where = prompt:K2_GetActorLocation()
            local metres = Distance(where.X, where.Y, where.Z)
            Detail(function() return string.format("prompt: %s at %.2f m", prompt:GetClass():GetFName():ToString(),
                metres) end)
        end
        if Settings.Chests and Settings.Mode == "All" and Valid(found.chestClass) and prompt:IsA(found.chestClass) then
            local name = prompt:GetFullName()
            if not opened[name] and not Recent(name, now) then
                target, key, chest = prompt, name, true
                local where = prompt:K2_GetActorLocation()
                distance = Distance(where.X, where.Y, where.Z)
            end
        end
    else
        lastPrompt = nil
    end
    if not target then
        -- The nearest pile within Range that was not tried in the last RETRY_SECONDS.
        local best, bestDistance = nil, nil
        for _, entry in ipairs(piles) do
            local metres = Distance(entry.x, entry.y, entry.z)
            if metres <= Settings.Range and (not bestDistance or metres < bestDistance)
                and not Recent(entry.name, now) then
                best, bestDistance = entry, metres
            end
        end
        if best and Same(best) then
            target, key, distance = best.actor, best.name, bestDistance
        elseif best then
            -- Gone since the list (taken, or streamed out): list again at the next check.
            tried[best.name] = now
            listDue = true
        end
    end
    if not target then
        return
    end

    if Blocked(player) then
        return
    end
    -- Before the loot checks, so a skipped target is looked at once per RETRY_SECONDS, not on every check.
    tried[key] = now
    local path = key:match("^%S+%s+(.+)$") or key
    local why
    if chest then
        why = ChestReason(target)
    else
        why = PileReason(target)
    end
    if why then
        counts.skipped = counts.skipped + 1
        Detail(function() return string.format("left for the player: %s at %.1f m: %s", Short(path), distance, why) end)
        return
    end
    if not Valid(target) then
        return
    end
    if chest then
        opened[key] = true
        counts.opened = counts.opened + 1
    else
        counts.picked = counts.picked + 1
    end
    Detail(function() return string.format("%s %s at %.1f m", chest and "open chest" or "pick up", Short(path),
        distance) end)
    interact:DoInteract(target)
end

---------------------------------------------------------------------------------------------------------------------
-- Start
---------------------------------------------------------------------------------------------------------------------

Settings = CopyDefaults()
Log(VERSION .. " loaded.")
local read, problem = pcall(ReadConfig)
if not read then
    LogError("Error while reading config.lua: " .. tostring(problem))
end
if not Settings.Enable then
    Log("Enable = false in config.lua: the mod does nothing until it is true.")
end

local function CheckStep()
    if stopped then
        return
    end
    checks = checks + 1
    local ok, err = pcall(Check)
    if not ok then
        checkErrors = checkErrors + 1
        LogError("Error: " .. tostring(err))
        if checkErrors >= STOP_AFTER_ERRORS then
            stopped = true
            Log("Off until the next game start (" .. STOP_AFTER_ERRORS .. " errors): the game plays as normal.")
        end
    end
    if checks % STATUS_EVERY == 0 then
        Detail(function() return string.format("status: %d piles listed, picked up %d, chests opened %d, left for "
            .. "the player %d", #piles, counts.picked, counts.opened, counts.skipped) end)
    end
end

local function ConfigStep()
    local ok, err = pcall(ReadConfig)
    if not ok then
        LogError("Error while reading config.lua: " .. tostring(err))
    end
end

RegisterLoadMapPostHook(function()
    local ok, err = pcall(function()
        -- The old world's actors and subsystems are gone: drop them without touching them.
        controller, interact, interactOwner, backpack, events, lastPrompt = nil, nil, nil, nil, nil, nil
        piles = {}
        searchDue, listDue = true, true
    end)
    if not ok then
        LogError("Error after the map load: " .. tostring(err))
    end
end)

-- Both loops run on the game thread (UE4SS's Delayed Action API): no Lua on a second thread (Witch Time's crash
-- lesson, knowledge\engine\ue4ss.md). A UE4SS without it gets the old LoopAsync loops, and the log says so.
if type(LoopInGameThreadWithDelay) == "function" then
    LoopInGameThreadWithDelay(CHECK_INTERVAL_MS, CheckStep)
    LoopInGameThreadWithDelay(CONFIG_INTERVAL_MS, ConfigStep)
else
    Log("This UE4SS has no game-thread loops (LoopInGameThreadWithDelay): the older LoopAsync is used; a newer UE4SS "
        .. "is safer.")
    LoopAsync(CHECK_INTERVAL_MS, function()
        ExecuteInGameThread(CheckStep)
        -- false keeps the loop running.
        return false
    end)
    LoopAsync(CONFIG_INTERVAL_MS, function()
        ExecuteInGameThread(ConfigStep)
        return false
    end)
end
