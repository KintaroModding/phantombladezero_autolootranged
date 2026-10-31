# Auto Loot (Ranged)

Loot piles near you are picked up by themselves from a short distance; chests open when the game's prompt targets them. Gear choices, story items and scripted loot stay for the player. Settings in config.lua, live in game.

A [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) Lua mod for Phantom Blade Zero (Unreal Engine 5.7), made with
game version `CL-301524_JKS-17219` and UE4SS `v3.0.1-1188-gf885e8d2`. This README is for developers; what the mod does and
how to install it are in [openmods.md](openmods.md), the mod page on [openmods](https://openmods.nexus).

## How it works

Every 0.25 seconds, on the game thread, `Scripts/main.lua` picks one target and calls the game's own interact call on
it, `PBZInteractComponent:DoInteract`, the call a key press makes. The pickup, the chest's opening, the bag, the save
flag and the popup then run as in the game.

| Target | When |
|---|---|
| Loot pile (`BP_Rubbish_C` and its children, also notes and pages) | the nearest one within `Range` metres of the player |
| Chest (`BP_Chest_C`, `Chests = true`, `Mode = "All"`) | only when the game's interact prompt targets it (`GetInteractActor`), once per game start: its opening montage moves the player to the chest |

The piles are listed every 2 seconds (`GameplayStatics:GetAllActorsOfClass`) with their locations; each check
measures the distance in Lua. A kept pile is used only after its address and full name match again.

Before the call, the mod checks the game's state: not paused, no other interaction or sequence, no pickup popup, no
dialogue (`EventPipelineManager.DialogueWidget`), none of the player's tags `CharacterState.InCombat`, `.Dead`,
`.Space.Air`, `.StatusInfliction`, `.Action` (a parent tag counts its children). Then the target's own checks:

- Pile: not taken (`Not Interact`, `bDestroyed`), no flow of its own (`BP_PBZFlowComponent.RootFlow`), no level
  sequence, no `AddTagWhenSuccess`; after its `Collect Items`: no weapon, medicine or accessory (the game's equip
  choice).
- Chest: not open (`CurSwitchState`), no flow of its own, no sequence other than the stock `LS_Interact_Chest`, no
  `RequiredItems` (a key item), no story tags; after its `Collect Items`.
- Each item (`PBZBackPackSubsystem:GetPropData`): not a narrative item (level or category), no memory flower or petal,
  no tag when owned (`AddTagWhenHas`). `Mode = "Materials"`: only materials, no chests. `Mode = "Common"`: also only
  the item levels C, D, E, F, B, B1-B3 (a white or pale blue glow).

A target is tried at most once in 10 seconds. After 3 errors the mod turns itself off until the next game start.

## Deploy and test

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy.ps1     # copy the mod folder into the game's ue4ss\Mods
python tests\test_main.py                                 # offline test of main.lua (needs lupa)
```

`deploy.ps1` copies `src\phantombladezero_autolootranged\` to `<game folder>\platform\steam\ue4ss\Mods\phantombladezero_autolootranged\`. The game
folder is `$env:UE5_GAMES_DIR\PBZ`; `-GameDir` or `-ModsDir` override it. The mod folder holds
`enabled.txt`, so UE4SS starts it without an entry in `mods.txt`. An existing `Scripts\config.lua` in the game stays
(the tester's settings); `-ResetConfig` copies the shipped one. UE4SS loads Lua mods at game start. The mod writes
its lines into `ue4ss\UE4SS.log`, for example `[Auto Loot (Ranged)] 1.0 loaded.`

`tests\test_main.py` runs `main.lua` in Lua 5.4 ([lupa](https://pypi.org/project/lupa/)) with mocks of the UE4SS
functions that the script uses. It finds syntax and logic errors before a game start.

Versioning: every change raises the second number (1.0 -> 1.1) in `mod.json` and in `main.lua`:
`python scripts\bump_version.py`.

## Settings

`Scripts/config.lua` holds the player's settings, `DEFAULTS` in `main.lua` every setting with its default, `LIMITS`
the range of the number settings and `CHOICES` the values of the text settings. The mod reads the file at start and
again every 5 seconds (`ReadConfig`): a changed file applies at the next check, a broken or half-saved one keeps the
last settings, a missing one means the defaults. Players and mod managers keep their own `config.lua` across updates,
so a new `main.lua` can meet an older file: a setting that the file lacks, or has with another type, keeps its
default, and the log says so (`ParseConfig`). Add each setting to
both files with the same value, and read it only from `Settings`, never as `config.X or default` (for a true/false
setting, `false or true` is true). `tests\test_main.py` checks that `config.lua` ships exactly the defaults, and runs
`main.lua` with a missing, empty, broken and wrong `config.lua`.

## Project layout

```
src/phantombladezero_autolootranged/   The UE4SS mod folder: Scripts/main.lua, Scripts/config.lua, enabled.txt
tests/test_main.py    Offline test of main.lua with mocks of UE4SS and the game
docs/                 Cover and screenshots
scripts/              bump_version.py
deploy.ps1            Copies the mod folder into the game's ue4ss\Mods
mod.json              Name, version, game, engine and UE4SS version
```

## License

MIT, see [LICENSE](LICENSE).
