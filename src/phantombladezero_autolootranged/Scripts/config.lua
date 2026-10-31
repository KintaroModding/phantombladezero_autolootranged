-- Settings of Auto Loot (Ranged). Change a value and save the file: the mod reads it again every 5 seconds, so the
-- change shows in the game without a restart. A file it cannot read (for example while it is saved) keeps the last
-- settings. A setting that this file does not have keeps its default, so an older copy of the file still works after
-- an update.
return {
    Enable = true,     -- false: the game as normal, every pickup by a key press
    Range = 4,         -- metres around you in which loot piles are picked up by themselves (0.5 to 10)
    Chests = true,     -- true: a chest opens by itself when the game's prompt shows on it (only in Mode "All")
    Mode = "All",      -- "All": piles, notes and chests; "Materials": only piles of materials, no chests;
                       -- "Common": like "Materials", but only materials with a white or pale blue glow
    LogDetail = false, -- true: ue4ss\UE4SS.log lists each pickup, each pile left for you and why (for tests)
}
