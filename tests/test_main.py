"""Offline test of Scripts/main.lua with lupa (Lua 5.4) and small mocks of UE4SS and the game.

The mocks stand in for the UE4SS functions that the script uses (the game-thread loops LoopInGameThreadWithDelay, the
old LoopAsync, RegisterLoadMapPostHook, StaticFindObject, FName, UEHelpers) and for a small loot world: the player with
its ability system (tags; a parent tag counts its children, as in UE), the interact component (its prompt target and
DoInteract), the backpack and event subsystems, loot piles (BP_Rubbish_C) and chests (BP_Chest_C) with their loot maps,
and the item data (GetPropData). As in UE4SS: the items of an array out parameter and the keys and values of a TMap's
ForEach are params (:get() is the value), a TArray has #, and a struct that a function returns is a Lua table.

The checks: the loops; a pile in Range is picked up, one outside is not, the nearest first; each reason a pile or a
chest stays with the player; chests only by the prompt and once; the modes; every state that blocks; the retry time;
live settings; a pile that is gone; the stop after errors; LogDetail lines (the prompt's distance). Then the settings
files a player may have instead of the shipped config.lua: main.lua must start, and every setting the file does not
give keeps its default; the shipped config.lua must hold exactly the defaults of main.lua.

Usage:
    python tests/test_main.py        (needs lupa: python -m pip install lupa)

Exit code: 0 = all checks passed, 1 = a check failed.
"""

import json
import sys
import tempfile
from pathlib import Path

from lupa import lua54

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "src" / "phantombladezero_autolootranged" / "Scripts" / "main.lua"
CONFIG = MAIN.parent / "config.lua"

MOCKS = r"""
LOG = {}
print = function(...) LOG[#LOG + 1] = table.concat({...}, "") end
CLOCK = 100
os.clock = function() return CLOCK end

local FNameMeta = { __index = { ToString = function(self) return self.text end } }
EFindName = { FNAME_Find = 0, FNAME_Add = 1 }
function FName(text, findType) return setmetatable({ text = text }, FNameMeta) end

LOOPS = {}
DELAYS = {}
ASYNC_LOOPS = 0
function LoopInGameThreadWithDelay(ms, callback) LOOPS[#LOOPS + 1] = callback DELAYS[#DELAYS + 1] = ms return #LOOPS end
function LoopAsync(ms, callback) ASYNC_LOOPS = ASYNC_LOOPS + 1 LOOPS[#LOOPS + 1] = callback DELAYS[#DELAYS + 1] = ms end
function ExecuteInGameThread(callback) callback() end
MAP_HOOKS = {}
function RegisterLoadMapPostHook(callback) MAP_HOOKS[#MAP_HOOKS + 1] = callback end
function RUN(ms) for i = 1, #LOOPS do if DELAYS[i] == ms then LOOPS[i]() end end end
-- One check every 0.25 s of game time.
function CHECK(n) for _ = 1, n or 1 do CLOCK = CLOCK + 0.25 RUN(250) end end
function CONFIG() RUN(5000) end

-- Objects: fields are properties, plus the UObject methods the script uses.
NEXT_ADDRESS = 4096
local ObjectMeta = { __index = {
    IsValid = function(self) return self.valid ~= false end,
    GetAddress = function(self) return self.address end,
    GetFullName = function(self) return self.class .. " " .. self.path end,
    GetFName = function(self) return FName(self.name) end,
    GetClass = function(self) return { GetFName = function() return FName(self.class) end } end,
    IsA = function(self, class) return (self.classes or {})[class] == true end,
    K2_GetActorLocation = function(self) return { X = self.at[1], Y = self.at[2], Z = self.at[3] } end,
} }
function Object(class, path, fields)
    local object = fields or {}
    object.class, object.path, object.name = class, path, path:match("[^.:/]+$")
    NEXT_ADDRESS = NEXT_ADDRESS + 16
    object.address = NEXT_ADDRESS
    return setmetatable(object, ObjectMeta)
end
INVALID = setmetatable({ valid = false, class = "None", path = "None", name = "None", address = 0 }, ObjectMeta)

-- A param (RemoteUnrealParam): :get() is the value, nothing else.
local ParamMeta = { __index = function(self, key)
    if key == "get" then return function(param) return param.value end end
    error("Param has no member " .. tostring(key))
end }
function Param(value) return setmetatable({ value = value }, ParamMeta) end
local ArrayMeta = { __len = function(self) return #self.items end,
    __index = function(self, i) return self.items[i] end }
function TArray(items) return setmetatable({ items = items or {} }, ArrayMeta) end
-- A TMap: ForEach(fn(key, value)) with params; a true return stops it, as in UE4SS.
local MapMeta = { __index = { ForEach = function(self, fn)
    for _, pair in ipairs(self.pairs) do
        if fn(Param(pair[1]), Param(pair[2])) == true then break end
    end
end } }
function TMap(pairs) return setmetatable({ pairs = pairs or {} }, MapMeta) end
function Loot(...)
    local pairs = {}
    for _, id in ipairs({...}) do pairs[#pairs + 1] = { FName(id), 1 } end
    return TMap(pairs)
end

-- Item data by id: { level, detail type, item type, tag when owned }.
ITEMS = {
    Herb = { 0, 1, 0, "None" },          -- a common material (level C)
    Ore = { 5, 1, 0, "None" },           -- a common material (level D)
    RareOre = { 2, 1, 0, "None" },       -- a purple material (level A)
    Page = { 0, 0, 2, "None" },          -- a book page, not a material
    Letter = { 4, 0, 3, "None" },        -- a narrative item
    Flower = { 0, 0, 4, "None" },        -- a memory flower
    Token = { 0, 0, 0, "Quest.Token" },  -- sets a quest tag when owned
}
PROP_CALLS = 0
BACKPACK_DEFAULT = Object("PBZBackPackSubsystem", "/Script/ProjectPBZ.Default__PBZBackPackSubsystem", {
    GetPropData = function(self, id)
        PROP_CALLS = PROP_CALLS + 1
        if PROP_FAIL then error("GetPropData failed") end
        local d = ITEMS[id:ToString()]
        return { ItemLevel = d[1], ItemDetailType = d[2], ItemType = d[3], AddTagWhenHas = { TagName = FName(d[4]) } }
    end })

STOCK_SEQUENCE = Object("LevelSequence", "/Game/PBZContent/Sequencer/Switch/LS_Interact_Chest.LS_Interact_Chest")
OTHER_SEQUENCE = Object("LevelSequence", "/Game/PBZContent/Sequencer/Story/LS_Story_01.LS_Story_01")

PILE = Object("BlueprintGeneratedClass", "/Game/PBZContent/Blueprint/Item/Device/BP_Rubbish.BP_Rubbish_C")
CHEST = Object("BlueprintGeneratedClass", "/Game/PBZContent/Blueprint/Item/Device/BP_Chest.BP_Chest_C")
local LEVEL = "/Game/Maps/B02/PBZ_Xigu_WP.PBZ_Xigu_WP:PersistentLevel."
local function Flow(scripted)
    return Object("BP_PBZFlowComponent_C", LEVEL .. "Flow", { RootFlow = scripted and Object("FlowAsset", "/Game/F.F")
        or INVALID })
end
local function Tags(...) local t = {} for _, n in ipairs({...}) do t[#t + 1] = { TagName = FName(n) } end
    return { GameplayTags = TArray(t) } end
-- A pile at x metres from the player (on the X axis); o: changes to its fields.
function Pile(name, x, loot, o)
    o = o or {}
    local pile = Object("BP_Rubbish_C", LEVEL .. name, {
        at = { x * 100, 0, 0 }, classes = { [PILE] = true },
        ["Not Interact"] = false, bDestroyed = false, BP_PBZFlowComponent = Flow(o.scripted),
        LevelSequenceAsset = o.sequence or INVALID, AddTagWhenSuccess = o.storyTag and Tags("Story.Done") or Tags(),
        ["Collect Items"] = function(self) self.collected = (self.collected or 0) + 1 end,
        ReallyGetWeapons = TArray(o.weapon and { 1 } or {}), ReallyMedicines = TArray(), ReallyAccessories = TArray(),
        PickupMap = loot,
    })
    return pile
end
function Chest(name, x, loot, o)
    o = o or {}
    return Object("BP_Chest_C", LEVEL .. name, {
        at = { x * 100, 0, 0 }, classes = { [CHEST] = true },
        CurSwitchState = false, BP_PBZFlowComponent = Flow(false),
        LevelSequenceAsset = o.sequence or STOCK_SEQUENCE,
        RequiredItems = o.locked and TMap({ { FName("Key"), 1 } }) or TMap(),
        AddTagWhenSuccess = Tags(), Tags_CompletedInteract = Tags(),
        ["Collect Items"] = function(self) self.collected = (self.collected or 0) + 1 end,
        PickupMaps = loot,
    })
end

-- The player: a location and an ability system with tags (a parent tag counts its children, as in UE).
PLAYER_TAGS = {}
ABILITIES = Object("AbilitySystemComponent", LEVEL .. "Hun.Abilities", {
    GetGameplayTagCount = function(self, tag)
        local n, wanted = 0, tag.TagName:ToString()
        for _, t in ipairs(PLAYER_TAGS) do
            if t == wanted or t:sub(1, #wanted + 1) == wanted .. "." then n = n + 1 end
        end
        return n
    end })
PLAYER = Object("BP_Renewal_Hun_C", LEVEL .. "BP_Renewal_Hun_C_0", { at = { 0, 0, 0 }, AbilitySystemComponent = ABILITIES })
CONTROLLER = Object("PlayerController", LEVEL .. "PlayerController_0", { Pawn = PLAYER })

-- The interact component: the prompt's target, another interaction, a sequence; DoInteract takes a pile or opens a
-- chest like the game.
INTERACTED = {}
INTERACT = Object("PBZInteractComponent", LEVEL .. "BP_Renewal_Hun_C_0.PBZInteract", {
    prompt = INVALID, interacting = INVALID, sequence = false,
    GetInteractActor = function(self) return self.prompt end,
    GetInteractingActor = function(self) return self.interacting end,
    HasValidLevelSequencePlayer = function(self) return self.sequence end,
    DoInteract = function(self, target)
        INTERACTED[#INTERACTED + 1] = target.name
        if target.classes[CHEST] then target.CurSwitchState = true else target["Not Interact"] = true end
    end })
INTERACT_DEFAULT = Object("PBZInteractComponent", "/Script/ProjectPBZ.Default__PBZInteractComponent", {
    GetInteractComponent = function(self, player) return INTERACT end })

BACKPACK_CLASS = Object("Class", "/Script/ProjectPBZ.PBZBackPackSubsystem")
EVENTS_CLASS = Object("Class", "/Script/ProjectPBZ.EventPipelineManager")
BACKPACK = Object("PBZBackPackSubsystem", "/Game/Maps/B02.PBZBackPackSubsystem_0", { popup = false,
    IsActivatePickupWidget = function(self) return self.popup end,
    IsDisplayPickupUI = function(self) return false end })
EVENTS = Object("EventPipelineManager", "/Game/Maps/B02.EventPipelineManager_0", { DialogueWidget = INVALID })
SUBSYSTEMS = Object("SubsystemBlueprintLibrary", "/Script/Engine.Default__SubsystemBlueprintLibrary", {
    GetWorldSubsystem = function(self, context, class)
        if class == BACKPACK_CLASS then return BACKPACK end
        if class == EVENTS_CLASS then return EVENTS end
        return INVALID
    end })

OBJECTS = {}
SEARCHES = 0
function StaticFindObject(path) SEARCHES = SEARCHES + 1 return OBJECTS[path] or INVALID end
for _, object in ipairs({ PILE, CHEST, INTERACT_DEFAULT, BACKPACK_CLASS, BACKPACK_DEFAULT, EVENTS_CLASS, SUBSYSTEMS }) do
    OBJECTS[object.path] = object
end

ACTORS = { [PILE] = {} }
PAUSED = false
STATICS = { calls = 0 }
function STATICS:GetAllActorsOfClass(context, class, out)
    self.calls = self.calls + 1
    for _, actor in ipairs(ACTORS[class] or {}) do out[#out + 1] = Param(actor) end
end
function STATICS:IsGamePaused(context) return PAUSED end
CONTROLLER_CALLS = 0
package.preload["UEHelpers"] = function()
    return {
        GetPlayerController = function() CONTROLLER_CALLS = CONTROLLER_CALLS + 1 return CONTROLLER end,
        GetGameplayStatics = function() return STATICS end,
    }
end

-- The names DoInteract got, comma-separated.
function DONE() return table.concat(INTERACTED, ",") end

function DESCRIBE(t)
    local names = {}
    for name in pairs(t) do names[#names + 1] = tostring(name) end
    table.sort(names)
    local parts = {}
    for i, name in ipairs(names) do parts[i] = name .. "=" .. tostring(t[name]) end
    return table.concat(parts, ", ")
end
"""

# The files a player may have instead of the shipped config.lua: (case, content or None for no file, a text that
# main.lua must log). "wrong types" is made from the shipped file: every value gets another type.
PLAYER_CONFIGS = [
    ("no config.lua", None, "No config.lua"),
    ("an empty or older config.lua", "return {}\n", None),
    ("a broken config.lua", "return {\n    Enable =\n", "config.lua not read"),
    ("a config.lua without a table", "-- nothing here\n", "does not return a table"),
    ("a config.lua with wrong types", "wrong types", "must be a"),
    ("a config.lua with an unknown setting", "return { NoSuchSetting = 1 }\n", "unknown setting NoSuchSetting"),
]


def lua_path(path: Path) -> str:
    return "[[" + path.as_posix() + "]]"


class Run:
    """main.lua in a fresh Lua state, with config.lua read from config_folder; world: Lua that sets up the loot."""

    def __init__(self, config_folder: Path, world: str = ""):
        self.folder = config_folder
        self.lua = lua54.LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(MOCKS)
        self.lua.execute(f'package.path = {lua_path(config_folder)} .. "/?.lua"; package.cpath = ""')
        if world:
            self.lua.execute(world)
        self.g = self.lua.globals()
        self.do(MAIN.read_text(encoding="utf-8"))

    def do(self, code: str):
        """Runs Lua code; a Lua error that stops it becomes an "Error" line in the log, so the checks fail."""
        try:
            return self.lua.execute(code)
        except lua54.LuaError as error:
            self.g.LOG[len(self.g.LOG) + 1] = "Error: main.lua stopped: " + str(error).splitlines()[0]

    def eval(self, expression: str):
        return self.lua.eval(expression)

    def log(self):
        return [self.g.LOG[i].rstrip() for i in range(1, len(self.g.LOG) + 1)]

    def write_config(self, text: str):
        (self.folder / "config.lua").write_text(text, encoding="utf-8")


def settings_lines(log):
    return [line.split("] Settings: ", 1)[1] for line in log if "] Settings: " in line]


# A world with piles at several distances (metres from the player) and one chest.
WORLD = """
    W = {}
    W.near = Pile("BP_Rubbish_C_1", 2.0, Loot("Herb", "Ore"))
    W.nearer = Pile("BP_Rubbish_C_2", 1.0, Loot("Page"))
    W.far = Pile("BP_Rubbish_C_3", 5.0, Loot("Herb"))
    W.chest = Chest("BP_Chest_C_1", 1.5, Loot("Herb", "RareOre"))
    ACTORS[PILE] = { W.near, W.nearer, W.far }
"""


def main() -> None:
    version = json.loads((ROOT / "mod.json").read_text(encoding="utf-8"))["version"]
    failures = []

    def check(condition, text):
        print(("ok    " if condition else "FAIL  ") + text)
        if not condition:
            failures.append(text)

    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        shipped_text = CONFIG.read_text(encoding="utf-8")

        def folder(name, text=shipped_text):
            f = temp / name
            f.mkdir()
            if text is not None:
                (f / "config.lua").write_text(text, encoding="utf-8")
            return f

        detail_text = shipped_text.replace("LogDetail = false,", "LogDetail = true, ")

        # --- The mod as players get it.
        r = Run(folder("shipped"), WORLD)
        log = r.log()
        for line in log:
            print("      log: " + line)
        check(list(r.g.DELAYS.values()) == [250, 5000] and r.g.ASYNC_LOOPS == 0 and len(r.g.MAP_HOOKS) == 1,
              "game-thread loops: the check every 0.25 s, the config read every 5 s; one map load hook")
        check(any(f"{version} loaded." in line for line in log), f"load line with the version {version} of mod.json")
        shipped = r.eval(f"DESCRIBE(dofile({lua_path(CONFIG)}))")
        check(settings_lines(log) == [shipped] and not any("config.lua" in line for line in log),
              "one settings line with the shipped values, no warning")

        e = r.eval
        r.do("CHECK()")
        check(e("DONE()") == "BP_Rubbish_C_2", "the nearest pile in Range (1 m) first")
        r.do("CHECK()")
        check(e("DONE()") == "BP_Rubbish_C_2,BP_Rubbish_C_1", "then the next one (2 m)")
        r.do("CHECK(20)")
        check(e("DONE()") == "BP_Rubbish_C_2,BP_Rubbish_C_1", "a pile at 5 m (outside the 4 m Range) stays")
        check(e("W.chest.CurSwitchState") is False, "a chest at 1.5 m without the game's prompt stays closed")
        r.do("INTERACT.prompt = W.chest CHECK()")
        check(e("DONE()").endswith(",BP_Chest_C_1") and e("W.chest.CurSwitchState") is True,
              "the chest opens when the game's prompt targets it (gear and rare loot allowed in All)")
        r.do("W.chest.CurSwitchState = false CLOCK = CLOCK + 60 CHECK(4)")
        check(e("DONE()").count("BP_Chest_C_1") == 1, "a chest is opened at most once per game start")
        r.do("INTERACT.prompt = INVALID")
        check(e("SEARCHES") == 7 and e("CONTROLLER_CALLS") == 1,
              "each class and default object is searched once; the controller once")
        calls = e("STATICS.calls")
        r.do("CHECK(16)")
        check(e("STATICS.calls") - calls == 2, "the piles are listed every 8 checks (2 s), not on every check")

        # --- Live settings: Range and Enable.
        r.write_config(shipped_text.replace("Range = 4,", "Range = 6,"))
        r.do("CONFIG() CHECK()")
        check(any("Range=6" in line for line in settings_lines(r.log())) and e("DONE()").endswith(",BP_Rubbish_C_3"),
              "Range 6 (live): the pile at 5 m is picked up")
        r.do("W.late = Pile('BP_Rubbish_C_9', 1.0, Loot('Herb')) table.insert(ACTORS[PILE], W.late)")
        r.write_config(shipped_text.replace("Enable = true,", "Enable = false,"))
        r.do("CONFIG() CHECK(20)")
        check("BP_Rubbish_C_9" not in e("DONE()"), "Enable = false (live): nothing is picked up")
        r.write_config(shipped_text)
        r.do("CONFIG() CHECK(9)")
        check(e("DONE()").endswith(",BP_Rubbish_C_9"), "Enable = true again: picked up")
        r.write_config("return {\n    Range = 1,\n")
        r.do("CONFIG()")
        check(any("config.lua not read (line" in line and "the last settings stay" in line for line in r.log()),
              "a broken (half-saved) file keeps the last settings")
        r.write_config('return { Range = 50, Mode = "Everything" }\n')
        r.do("CONFIG()")
        check(any("Range = 50 is outside 0.5 to 10; 10 is used" in line for line in r.log())
              and any('Mode = "Everything" is not one of All, Materials, Common; the default All is used' in line
                      for line in r.log()), "a range out of limits is clamped; an unknown mode means the default")
        r.write_config('return { Mode = "materials" }\n')
        r.do("CONFIG()")
        check(any("Mode=Materials" in line for line in settings_lines(r.log())), "a mode in any case is taken")

        # --- What stays with the player (each in its own world, all at 1 m).
        def stays(world, text, config=shipped_text, prompt=None):
            run = Run(folder(f"stay{len(failures)}_{abs(hash(text))}", config), world)
            run.do((f"INTERACT.prompt = {prompt} " if prompt else "") + "CHECK(3)")
            check(run.eval("DONE()") == "", text)
            return run

        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb'), { weapon = true }) }", "a pile with a weapon stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb'), { scripted = true }) }", "a pile with its own flow stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb'), { sequence = STOCK_SEQUENCE }) }",
              "a pile with a sequence stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb'), { storyTag = true }) }", "a pile that sets a story tag stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb', 'Letter')) }", "a pile with a narrative item stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Flower')) }", "a pile with a memory flower stays")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Token')) }", "a pile with an item that sets a quest tag stays")
        stays("local p = Pile('P', 1, Loot('Herb')) p['Not Interact'] = true ACTORS[PILE] = { p }",
              "a taken pile stays")
        stays("W = {} W.c = Chest('C', 1, Loot('Herb'), { locked = true })", "a locked chest stays",
              prompt="W.c")
        stays("W = {} W.c = Chest('C', 1, Loot('Herb'), { sequence = OTHER_SEQUENCE })",
              "a chest with its own sequence stays", prompt="W.c")
        stays("W = {} W.c = Chest('C', 1, Loot('Herb', 'Letter'))", "a chest with a narrative item stays",
              prompt="W.c")
        stays("W = {} W.c = Chest('C', 1, Loot('Herb'))", "Chests = false: the prompt's chest stays",
              config=shipped_text.replace("Chests = true,", "Chests = false,"), prompt="W.c")
        stays("W = {} W.c = Chest('C', 1, Loot('Herb'))", "Mode Materials: no chests",
              config=shipped_text.replace('Mode = "All",', 'Mode = "Materials",'), prompt="W.c")
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb', 'Page')) }", "Mode Materials: a pile with a page stays",
              config=shipped_text.replace('Mode = "All",', 'Mode = "Materials",'))
        stays("ACTORS[PILE] = { Pile('P', 1, Loot('Herb', 'RareOre')) }", "Mode Common: a pile with a rare material "
              "stays", config=shipped_text.replace('Mode = "All",', 'Mode = "Common",'))
        common = Run(folder("common", shipped_text.replace('Mode = "All",', 'Mode = "Common",')),
                     "ACTORS[PILE] = { Pile('P', 1, Loot('Herb', 'Ore')) }")
        common.do("CHECK()")
        check(common.eval("DONE()") == "P", "Mode Common: a pile of common materials is picked up")

        # --- States that block.
        for code, text in [("PAUSED = true", "the game is paused"),
                           ("PLAYER_TAGS = { 'CharacterState.InCombat' }", "in combat"),
                           ("PLAYER_TAGS = { 'CharacterState.Action.Phase.Act' }", "during an action (a child tag)"),
                           ("PLAYER_TAGS = { 'CharacterState.Space.Air' }", "in the air"),
                           ("BACKPACK.popup = true", "the pickup popup is open"),
                           ("EVENTS.DialogueWidget = Object('W', '/Game/W.W')", "a dialogue runs"),
                           ("INTERACT.interacting = Object('A', '/Game/A.A')", "another interaction runs"),
                           ("INTERACT.sequence = true", "a sequence plays")]:
            run = Run(folder(f"block{len(text)}_{abs(hash(text))}"), "ACTORS[PILE] = { Pile('P', 1, Loot('Herb')) }")
            run.do(code + " CHECK(3)")
            blocked = run.eval("DONE()") == ""
            run.do("PAUSED = false PLAYER_TAGS = {} BACKPACK.popup = false EVENTS.DialogueWidget = INVALID "
                   "INTERACT.interacting = INVALID INTERACT.sequence = false CHECK()")
            check(blocked and run.eval("DONE()") == "P", f"blocked while {text}; picked up after it")
        check(Run(folder("tags-ok"), "PLAYER_TAGS = { 'CharacterState.Native.Attitude.Sprinting' } "
                  "ACTORS[PILE] = { Pile('P', 1, Loot('Herb')) }").eval("(function() CHECK() return DONE() end)()")
              == "P", "a tag outside the list (sprinting) does not block")

        # --- Retry time, a pile that is gone, errors.
        retry = Run(folder("retry"), "W = { p = Pile('P', 1, Loot('Letter')) } ACTORS[PILE] = { W.p }")
        retry.do("CHECK(20)")
        once = retry.eval("W.p.collected")
        retry.do("CLOCK = CLOCK + 10 CHECK()")
        check(once == 1 and retry.eval("W.p.collected") == 2,
              "a pile that stays is looked at once per 10 s, not on every check")
        gone = Run(folder("gone"), "W = { p = Pile('P', 1, Loot('Herb')) } ACTORS[PILE] = { W.p }")
        # Listed while the game is paused (nothing picked up), then the address holds another object.
        gone.do("PAUSED = true CHECK() PAUSED = false W.p.path = W.p.path .. '_reused' ACTORS[PILE] = {} CHECK()")
        listed = gone.eval("STATICS.calls")
        gone.do("CHECK()")
        check(gone.eval("DONE()") == "" and listed == 1 and gone.eval("STATICS.calls") == listed + 1,
              "a kept pile whose name changed is not touched; the piles are listed again")
        err = Run(folder("errors"), "PROP_FAIL = true W = {} for i = 1, 5 do "
                  "table.insert(ACTORS[PILE], Pile('P' .. i, 1, Loot('Herb'))) end")
        err.do("CHECK(10)")
        log = err.log()
        prop = err.eval("PROP_CALLS")
        err.do("CHECK(10)")
        check(sum("Error:" in line for line in log) == 3 and any("Off until the next game start" in line for line in log)
              and err.eval("PROP_CALLS") == prop and err.eval("DONE()") == "",
              "3 errors: the mod turns itself off, nothing is picked up, no more checks")

        # --- LogDetail and the map load.
        d = Run(folder("detail", detail_text), WORLD + " W.near['Collect Items'] = W.near['Collect Items'] "
                "table.insert(ACTORS[PILE], Pile('BP_Rubbish_C_7', 2.5, Loot('Letter')))")
        d.do("INTERACT.prompt = W.nearer CHECK(4)")
        log = d.log()
        for line in log[-4:]:
            print("      log: " + line)
        check(any("classes: all found" in line for line in log) and any("listed BP_Rubbish: 4 piles" in line
                                                                          for line in log)
              and any("prompt: BP_Rubbish_C at 1.00 m" in line for line in log)
              and any("pick up BP_Rubbish_C_2 at 1.0 m" in line for line in log)
              and any("left for the player: BP_Rubbish_C_7 at 2.5 m: story item Letter" in line for line in log)
              and not any("Error" in line for line in log),
              "LogDetail: classes, the listed piles, the prompt's distance, pickups and the reason a pile stays")
        d.do("MAP_HOOKS[1]() CHECK()")
        check(d.eval("CONTROLLER_CALLS") == 2 and not any("Error" in line for line in d.log()),
              "after a map load the controller is searched again, without an error")

        # --- The settings files a player may have.
        folders = {}
        for case, content, _ in PLAYER_CONFIGS + [("Enable = false", "return { Enable = false }\n", None)]:
            if content == "wrong types":
                content = r.eval(f"""(function()
                    local lines = {{}}
                    for name, value in pairs(dofile({lua_path(CONFIG)})) do
                        lines[#lines + 1] = string.format("    [%q] = %s,", name, type(value) == "string" and "0" or '"wrong"')
                    end
                    return "return {{\\n" .. table.concat(lines, "\\n") .. "\\n}}\\n"
                end)()""")
            folders[case] = folder(f"player{len(folders)}", content)

        defaults = settings_lines(Run(folders["an empty or older config.lua"]).log())
        check(defaults == [shipped], f"config.lua ships exactly the defaults of main.lua ({defaults})")
        count = len(list(r.eval(f"dofile({lua_path(CONFIG)})").keys()))
        for case, _, expected in PLAYER_CONFIGS:
            run = Run(folders[case], "ACTORS[PILE] = { Pile('P', 1, Loot('Herb')) }")
            run.do("CHECK(2) CONFIG()")
            log = run.log()
            fine = (settings_lines(log) == defaults and len(run.g.LOOPS) == 2 and run.eval("DONE()") == "P"
                    and not any("Error" in line for line in log))
            if expected is None:
                fine = fine and not any("config.lua" in line for line in log)
            else:
                fine = fine and sum(expected in line for line in log) == (count if case.endswith("wrong types") else 1)
            check(fine, f"{case}: starts with the defaults and picks up"
                  + (f", logs '{expected}' once per setting" if case.endswith("wrong types")
                     else (f", logs '{expected}' once" if expected else ", no warning")))
            if not fine:
                for line in log:
                    print("      log: " + line)
        run = Run(folders["Enable = false"], "ACTORS[PILE] = { Pile('P', 1, Loot('Herb')) }")
        run.do("CHECK(3)")
        check(len(run.g.LOOPS) == 2 and run.eval("DONE()") == "" and run.eval("STATICS.calls") == 0
              and any("does nothing until it is true" in line for line in run.log()),
              "Enable = false at start: the loops run (live settings), nothing is listed or picked up")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
