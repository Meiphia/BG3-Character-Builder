import re
"""Character-creator view of the game database: the menus the in-game creator shows,
plus equipment per slot that fits the chosen race/body."""

from .gamedata import NULL_GUID

BODY_LABELS = {(1, 0): "Body 1 (feminine)", (0, 0): "Body 2 (masculine)",
               (1, 1): "Body 3 (strong feminine)", (0, 1): "Body 4 (strong masculine)"}

COLOR_TABLES = {"SkinColors": "CharacterCreationSkinColors", "EyeColors": "CharacterCreationEyeColors",
                "HairColors": "CharacterCreationHairColors"}  # everything else -> ColorDefinitions

COLOR_CATEGORIES = ("SkinColors", "EyeColors", "HairColors", "HairHighlightColors", "HairGrayingColors",
                    "MakeupColors", "LipsMakeupColors", "TattooColors", "HornColors", "HornTipColors")

APPEARANCE_SLOTS = ("Head", "Hair", "Beard", "Horns", "Wings", "Tail", "Private Parts",
                    "DragonbornTop", "DragonbornJaw", "DragonbornChin", "Piercing")

MATERIAL_KINDS = ("Tattoo", "Makeup", "Scar", "Passive")

# stats "Slot" values -> UI slot
EQUIP_SLOTS = {
    "Helmet": "Helmet", "Breast": "Chest", "Cloak": "Cloak", "Gloves": "Gloves", "Boots": "Boots",
    "Underwear": "Underwear", "VanityBody": "Camp Clothes", "VanityBoots": "Camp Shoes",
    "Amulet": "Amulet", "Ring": "Ring",
    "Melee Main Weapon": "Main Hand", "Melee Offhand Weapon": "Off Hand",
    "Ranged Main Weapon": "Ranged", "Ranged Offhand Weapon": "Ranged Off Hand",
}

HELD_SLOTS = ("Main Hand", "Off Hand", "Ranged", "Ranged Off Hand")   # drawn with their world model


def srgb_hex_to_linear(hexstr):
    h = hexstr.lstrip("#")
    if len(h) == 8:  # ARGB
        h = h[2:]
    rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    return [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]


def _nz(v):
    return v if v not in (None, "", NULL_GUID) else None


class Creator(object):
    def __init__(self, gd):
        self.gd = gd
        self.cc = gd.cc
        self._equip_cache = None

    # ------------------------------------------------------------ races / bodies

    def races(self):
        """Playable races from the creator presets: [{uuid, name, subraces, bodies}]"""
        out = {}
        for p in self.cc.get("CharacterCreationPresets", {}).values():
            race = p.get("RaceUUID")
            r = out.setdefault(race, {"uuid": race, "name": self.race_name(race), "subraces": {}, "bodies": {}})
            sub = _nz(p.get("SubRaceUUID"))
            if sub:
                r["subraces"][sub] = self.race_name(sub)
            key = (p.get("BodyType", 0), p.get("BodyShape", 0))
            r["bodies"].setdefault(key, {"body_type": key[0], "body_shape": key[1],
                                         "label": BODY_LABELS.get(key, str(key)), "presets": {}})
            r["bodies"][key]["presets"][sub or race] = {"preset": p.get("UUID"), "root_template": p.get("RootTemplate")}
        res = []
        for r in sorted(out.values(), key=lambda r: r["name"]):
            r["subraces"] = [{"uuid": k, "name": v} for k, v in sorted(r["subraces"].items(), key=lambda kv: kv[1])]
            r["bodies"] = [r["bodies"][k] for k in sorted(r["bodies"], key=lambda k: (k[1], -k[0]))]
            r["child"] = self.CHILD_RACES.get(r["name"], (None,))[0] is not None
            res.append(r)
        return res

    def race_name(self, uuid):
        r = self.cc.get("Races", {}).get(uuid)
        if not r:
            return uuid
        return self.gd.text(r.get("DisplayName")) or r.get("Name") or uuid

    def race_chain(self, race, subrace=None):
        chain, seen = [], set()
        cur = _nz(subrace) or race
        while cur and cur not in seen and cur in self.cc.get("Races", {}):
            seen.add(cur)
            chain.append(self.cc["Races"][cur])
            cur = _nz(self.cc["Races"][cur].get("ParentGuid"))
        return chain

    def race_list(self, race, subrace, category):
        """Most specific race in the chain that defines this list wins (subraces override)."""
        for r in self.race_chain(race, subrace):
            lst = r.get("_children", {}).get(category)
            if lst:
                return [o.get("Object") for o in lst]
        return []

    def preset_for(self, race, subrace, body_type, body_shape):
        for r in self.races():
            if r["uuid"] != race:
                continue
            for b in r["bodies"]:
                if (b["body_type"], b["body_shape"]) == (body_type, body_shape):
                    return b["presets"].get(_nz(subrace) or race) or next(iter(b["presets"].values()))
        return None

    # ------------------------------------------------------------ appearance

    # origin characters: (display name, character-visual name prefixes in order of preference)
    ORIGINS = (("Astarion", ("S_Player_Astarion_",)), ("Gale", ("S_Player_Gale_",)),
               ("Karlach", ("S_Player_Karlach_",)), ("Lae'zel", ("S_Player_Laezel", "ORIGIN_Laezel_")),
               ("Shadowheart", ("S_Player_ShadowHeart_", "ORIGIN_Shadowheart_")),
               ("Wyll", ("S_Player_Wyll", "ORIGIN_INTRO_Wyll_")),
               ("Halsin", ("S_Player_Halsin", "S_GLO_Halsin_", "Companion_Halsin_")),
               ("Jaheira", ("S_Player_Jaheira_",)), ("Minsc", ("S_Player_Minsc_",)),
               ("Minthara", ("S_Player_Minthara", "TEMP_Minthara_Naked_")))
    NPC_HEADS = (("CAMBION_F_NKD_Head_Mizora", "Mizora (cambion)", "mizora"),
                 ("HUM_F_NKD_Head_Mizora", "Mizora (disguise)", "mizora"),
                 ("CAMBION_M_NKD_Head_Incubus", "Haarlep (incubus)", None),
                 ("CAMBION_F_NKD_Head_Succubus", "Succubus", None),
                 ("CAMBION_F_NKD_Head_Succubus_Ascended", "Succubus (ascended)", None),
                 ("CAMBION_M_NKD_Head_A", "Cambion", None),
                 ("CAMBION_M_NKD_Head_A_Ascended", "Cambion (ascended)", None),
                 ("CAMBION_M_NKD_Head_Raphael", "Raphael (cambion)", "raphael"),
                 ("HUM_M_NKD_Head_Raphael", "Raphael (human form)", "raphael"),
                 ("HUM_MS_NKD_Head_Raphael_Remap", "Raphael (human form)", "raphael"),
                 ("HUM_M_NKD_Head_Wyll_Devil", "Wyll (devil-touched)", "wyll"),
                 ("TIF_M_NKD_Head_Zevlor", "Zevlor", None),
                 ("TIF_MS_NKD_Head_Zevlor_Remap", "Zevlor", None),
                 ("TIF_M_NKD_Head_Rolan", "Rolan", None),
                 ("TIF_MS_NKD_Head_Rolan_Remap", "Rolan", None),
                 ("HUM_M_NKD_Head_Abdirak", "Abdirak", None),
                 ("HUM_MS_NKD_Head_Abdirak_Remap", "Abdirak", None))
    NPC_HAIR = (("CAMBION_F_NKD_Hair_Mizora", "Mizora (cambion)", "mizora"),
                ("HUM_F_NKD_Hair_Mizora", "Mizora", "mizora"),
                ("HUM_M_NKD_Hair_Mizora", "Mizora", "mizora"),
                ("CAMBION_M_NKD_Hair_Raphael", "Raphael (cambion)", "raphael"),
                ("CAMBION_M_NKD_Hair_Short_A", "Cambion short", None))
    WINGS = (("HUM_FS_Wings", "Aylin's wings (Nightsong)", None),
             ("HUM_FS_Wings_FX", "Aylin's wings, radiant (Nightsong)", None),
             ("CAMBION_F_NKD_Wings_A", "Cambion wings (female)", None),
             ("CAMBION_M_NKD_Wings_A", "Cambion wings (male)", None),
             ("CAMBION_M_NKD_Wings_B", "Cambion wings B (male)", None),
             ("PIXIE_F_NKD_Wings_A", "Pixie wings", None))
    ORIGIN_EXTRA_HAIR = (("Shadowheart (Wrath)", "HAIR_HUM_F_Shadowheart_Wrath_A_Spring"),)

    # (label, template name or prefix, race, subrace (or None), item-name key, portrait token)
    ORIGIN_CHARACTERS = (
        ("Astarion", "ORIGIN_Astarion", "Elf", "High Elf", "Astarion", "astarion"),
        ("Gale", "ORIGIN_Gale", "Human", None, "Gale", "gale"),
        ("Gale (God)", "ORIGIN_Gale_God", "Human", None, "Gale", "gale"),
        ("Karlach", "ORIGIN_Karlach", "Tiefling", "Zariel", "Karlach", "karlach"),
        ("Lae'zel", "ORIGIN_Laezel", "Githyanki", None, "Laezel", "laezel"),
        ("Shadowheart", "ORIGIN_Shadowheart", "Half-Elf", "High", "ShadowHeart", "shadowheart"),
        ("Shadowheart (Wrath)", "ORIGIN_Shadowheart_Wrath", "Half-Elf", "High", "ShadowHeart", "shadowheart"),
        ("Wyll", "ORIGIN_Wyll", "Human", None, "Wyll", "wyll"),
        ("Wyll (Devil)", "ORIGIN_Wyll_Devil", "Human", None, "Wyll", "wyll"),
        ("Halsin", "Companion_Halsin", "Elf", "Wood", "Halsin", "halsin"),
        ("Jaheira", "S_Player_Jaheira", "Half-Elf", "High", "Jaheira", "jaheira"),
        ("Minsc", "S_Player_Minsc", "Human", None, "Minsc", "minsc"),
        ("Minthara", "Elves_Female_Drow_Minthara", "Drow", "Lolth", "Minthara", "minthara"),
        ("The Dark Urge", "ORIGIN_INTRO_DarkUrge", "Dragonborn", "White", "DarkUrge", "darkurge"),
    )
    # NPCs: (label, head visual, race, subrace, item-name key, wings visual or None)
    NPC_CHARACTERS = (
        ("Ketheric Thorm", "HEL_M_NKD_Head_Ketheric", "Half-Elf", "High", "Ketheric", None),
        ("Orin the Red", "HUM_F_NKD_Head_Orin", "Human", None, "Orin", None),
        ("Enver Gortash", "HUM_M_Head_Gortash", "Human", None, "Gortash", None),
        ("Raphael (human guise)", "HUM_M_NKD_Head_Raphael", "Human", None, "Raphael", None),
        ("Dame Aylin (Nightsong)", "HUM_FS_NKD_Head_Nightsong", "Human", None, "Nightsong", "HUM_FS_Wings"),
        ("Isobel Thorm", "HEL_F_NKD_Head_Isobel", "Half-Elf", "High", "Isobel", None),
        ("Mizora (disguised)", "HUM_F_NKD_Head_Mizora", "Human", None, "Mizora", None),
        ("Cazador Szarr", "ELF_M_NKD_Head_Cazador", "Elf", "High", "Cazador", None),
        ("Viconia DeVir", "ELF_F_NKD_Head_Viconia", "Drow", "Sharran", "Viconia", None),
        ("Sarevok Anchev", "HUM_MS_NKD_Head_Sarevok", "Human", None, "Sarevok", None),
        ("Lorroakan", "HUM_M_NKD_Head_Lorroakan", "Human", None, "Lorroakan", None),
        ("Volothamp Geddarm", "HUM_M_NKD_Head_Volo", "Human", None, "Volo", None),
        ("Elminster", "HUM_M_NKD_Head_Elminster", "Human", None, "Elminster", None),
        ("Zevlor", "TIF_M_NKD_Head_Zevlor", "Tiefling", "Asmodeus", "Zevlor", None),
        ("Rolan", "TIF_M_NKD_Head_Rolan", "Tiefling", "Asmodeus", "Rolan", None),
        ("Kagha", "ELF_F_NKD_Head_Kagha", "Elf", "Wood", "Kagha", None),
        ("Wulbren Bongle", "GNO_M_NKD_Head_Wulbren_Bongle", "Gnome", None, "Wulbren", None),
        ("Mol (child)", "TIF_CHD_NKD_Head_Mol", "Tiefling", "Asmodeus", "Mol", None),
        ("Yenna (child)", "S_GLO_Yenna", "Human", None, "Yenna", None),
        ("Young Shadowheart (child)", "HEL_CHD_NKD_Head_Shadowheart", "Half-Elf", "High", "Shadowheart_Child", None),
    )
    _NPC_CV_SKIP = re.compile(r"CINE|Portrait|Dead|Corpse|Ghost|Illusion|TEST|Puppet|Statue|Disguise|Trailer|Dream|"
                              r"Headless|Helper|Dummy|Zombie|Mindflayer|Slayer|Bat_|_Swarmed|TEMP_|CAMBION|Placeholder|DELETE|\[WIP\]", re.I)
    _ORIGIN_SKIP = re.compile(r"CINE|LOOT|BOOK|DYE|_Head|Headless|Portrait|Naked|TEMP_|Dummy|_Test", re.I)
    _BODY = {"F": (1, 0), "M": (0, 0), "FS": (1, 1), "MS": (0, 1)}

    # ------------------------------------------------------------ children
    # race -> (child body family, head prefixes); the game has child bodies for these
    CHILD_RACES = {"Human": ("HUM", ("HUM", "HEL")), "Half-Elf": ("HUM", ("HEL", "HUM")),
                   "Tiefling": ("TIF", ("TIF",)), "Githyanki": ("GTY", ("GTY",))}
    _CHILD_SKIP = re.compile(r"CINE|DELETE|Shadow_TWN|Dead|Corpse|Ghost|Puppet|Statue", re.I)

    def _child_pool(self):
        """Every child character the game defines, grouped by body: heads, hair, horns and whole outfits."""
        if getattr(self, "_children", None) is not None:
            return self._children
        pool = {}
        vis = self.gd.visuals
        for cid, cv in self.gd.charvisuals.items():
            name = cv.get("name") or ""
            body = (vis.get(cv.get("body_set_visual") or "") or {}).get("name") or ""
            m = re.match(r"^(HUM|TIF|GTY)_CHD_(?:NKD_)?Body_A$", body)
            if not m or self._CHILD_SKIP.search(name):
                continue
            fam = m.group(1)
            p = pool.setdefault(fam, {"base": None, "base_n": 99, "Head": {}, "Hair": {}, "Horns": {}, "Outfit": {}})
            slots = [(s_, v) for s_, v, b in cv.get("slots", []) if _nz(v) in vis]
            if len(slots) < p["base_n"] and (vis.get(cv.get("base_visual") or "") or {}).get("name", "").startswith(fam):
                p["base"], p["base_n"] = cid, len(slots)
            clothes = []
            for s_, v in slots:
                vn = vis[v].get("name") or ""
                if s_ == "Head" and "_CHD_" in vn and "Eyepatch" not in vn:
                    p["Head"].setdefault(v, vn)
                elif s_ == "Hair":
                    p["Hair"].setdefault(v, vn)
                elif s_ == "Horns" or "_Horns_" in vn:
                    p["Horns"].setdefault(v, vn)
                elif s_ not in ("Piercing",) and not vn.startswith("PRC_") and "Eyepatch" not in vn:
                    clothes.append(v)
            if clothes:
                key = tuple(sorted(clothes))
                if key not in {tuple(sorted(o[1])) for o in p["Outfit"].values()}:
                    label = re.sub(r"_[0-9a-f]{8}-[0-9a-f-]+$", "", name)
                    label = re.sub(r"^(S_[A-Z]{3}_|BASE_|CHD_|Child_)", "", label).replace("_", " ")
                    label = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", label).strip()
                    p["Outfit"][cid] = (label, clothes)
        self._children = pool
        return pool

    def child_family(self, race):
        name = self.race_name(race)
        return self.CHILD_RACES.get(name, (None, ()))

    def child_base(self, race):
        fam, _h = self.child_family(race)
        return ((self._child_pool().get(fam) or {}).get("base")) if fam else None

    def child_outfit_visuals(self, cid):
        for p in self._child_pool().values():
            if cid in p["Outfit"]:
                return p["Outfit"][cid][1]
        return []

    def child_menu(self, race, subrace):
        """Creator menu for a child: colours and markings as usual, but only child heads, hair, horns and
        outfits (taken from the game's own children)."""
        fam, heads = self.child_family(race)
        pool = self._child_pool()
        p = pool.get(fam) or {}
        bt, bs = 1, 0
        m = self.menu(race, subrace, bt, bs)
        def opts(d, group, pref=None):
            out = []
            for vid, vn in sorted(d.items(), key=lambda kv: kv[1]):
                if pref and not any(vn.startswith(h + "_CHD") for h in pref):
                    continue
                pretty = re.sub(r"^(HAIR_)?(CHD_)?([A-Z]{3}_)?(CHD_)?(NKD_)?", "", vn).replace("_", " ")
                out.append({"uuid": "origin:" + vid, "name": pretty, "visual": vid, "source": "origin",
                            "group": group, "internal": vn})
            return out
        hair = {}
        for q in pool.values():                 # child hair works across the human-shaped child bodies
            hair.update(q["Hair"])
        m["slots"] = {"Head": opts(p.get("Head", {}), "CHILD", heads), "Hair": opts(hair, "CHILD"),
                      "Horns": opts(p.get("Horns", {}), "CHILD") if fam == "TIF" else [],
                      "Outfit": []}
        outfits = dict(p.get("Outfit", {}))
        if fam in ("HUM", "TIF"):               # human and tiefling children share their clothes
            for f2 in ("HUM", "TIF"):
                outfits.update((pool.get(f2) or {}).get("Outfit", {}))
        for cid, (label, vids) in sorted(outfits.items(), key=lambda kv: kv[1][0]):
            m["slots"]["Outfit"].append({"uuid": "outfit:" + cid, "name": label, "visual": vids[0],
                                         "source": "origin", "group": "OUTFITS", "internal": "%d pieces" % len(vids)})
        m["child"] = True
        return m

    # creatures: built straight from the game's own character definition (their own skeleton and body),
    # (label, character visual name prefix, female?, {slot: visual name} overrides, [outfit visual names] or None)
    CREATURES = (
        ("Raphael (cambion form)", "QUEST_Monitor_DevilForm", False, {}, None),
        ("Raphael (archdevil)", "DEMON_Raphael_Body", False, {}, None),
        ("Haarlep", "S_LOW_HouseOfHope_Incubus", False, {}, None),
        ("Mizora (cambion)", "MizorasPact_Cambion_A", True,
         {"Head": "CAMBION_F_NKD_Head_Mizora", "Hair": "CAMBION_F_NKD_Hair_Mizora", "Wings": "CAMBION_F_NKD_Wings_A"},
         ["HUM_F_CLT_Mizora_Body_A", "HUM_F_CLT_Mizora_Belt_A", "HUM_F_CLT_Mizora_Cloak_A", "HUM_F_CLT_Mizora_Footwear_A",
          "HUM_F_CLT_Mizora_Necklace_A"]),
        ("Cambion", "S_LOW_HouseOfHope_RaphaelCambion01", False, {}, None),
        ("Orin (Bhaalspawn form)", "SLY_Bhaalspawn_Body_A", True, {}, None),
        ("The Emperor", "S_GLO_Emperor", False, {}, None),
        ("Mind flayer", "BASE_Mindflayer", False, {}, None),
        ("Mind flayer (ascended form)", "Mindflayer_Form_Player", False, {}, None),
        ("Prince Orpheus", "Orpheus", False, {}, None),
        ("Orthon", "ORTHON_Body_A", False, {}, None),
        ("Deva", "BASE_Deva_Male", False, {}, None),
        ("Imp", "IMP_Body_A", False, {}, None),
        ("Auntie Ethel (hag form)", "S_HAG_Hag_", True, {}, None),
    )

    def creature_characters(self):
        races = self.races()
        human = next((r for r in races if r["name"] == "Human"), races[0] if races else None)
        if not human:
            return []
        by_vname = {}
        for vid, v in self.gd.visuals.items():
            by_vname.setdefault(v.get("name"), vid)
        out = []
        for label, prefix, female, over, outfit in self.CREATURES:
            cands = sorted(((v.get("name") or "", k) for k, v in self.gd.charvisuals.items()
                            if (v.get("name") or "") == prefix or (v.get("name") or "").startswith(prefix)),
                           key=lambda c: (c[0] != prefix, "CINE" in c[0], len(c[0])))
            cands = [c for c in cands if self.gd.visuals.get(self.gd.charvisuals[c[1]].get("base_visual") or "")]
            if not cands:
                continue
            cid = cands[0][1]
            cv = self.gd.charvisuals[cid]
            slots = {sl: "origin:" + by_vname[vn] for sl, vn in over.items() if by_vname.get(vn)}
            head = next((v for sl, v in ((s_, v_) for s_, v_, b in cv.get("slots", [])) if sl == "Head"), None)
            if over.get("Head") and by_vname.get(over["Head"]):
                head = by_vname[over["Head"]]
            sel = {"race": human["uuid"], "subrace": (human["subraces"][0]["uuid"] if human["subraces"] else None),
                   "body_type": 1 if female else 0, "body_shape": 0, "creature_cv": cid, "origin_cv": cid,
                   "slots": slots, "equipment": {},
                   "toggles": {"tail": True, "nipple_covers": True, "modesty_leaf": True, "origin_markings": True,
                               "chest_physics": female}}
            if outfit:
                sel["creature_outfit"] = [by_vname[n] for n in outfit if by_vname.get(n)]
            out.append({"id": "creature:" + cid, "name": label, "portrait": None, "group": "CREATURES",
                        "head_visual": head, "selection": sel})
        return out

    def origin_characters(self):
        """Ready-made selections for the origin characters: their race and body, head, hair, horns and
        colours from the game's own character visual, and their starting outfit as real (swappable) items."""
        if getattr(self, "_origin_chars", None) is not None:
            return self._origin_chars
        chars = [(k, t) for k, t in self.gd.templates.items()
                 if t.get("Type") == "character" and _nz(t.get("CharacterVisualResourceID"))]
        races = self.races()
        items = self._items()
        out = []
        for label, tname, race_n, sub_n, key, portrait in self.ORIGIN_CHARACTERS:
            match = lambda n: n == tname or n.startswith(tname + "_")
            cands = [(t.get("Name") or "", t["CharacterVisualResourceID"]) for k, t in chars if match(t.get("Name") or "")]
            cands += [(v.get("name") or "", k) for k, v in self.gd.charvisuals.items()
                      if match(v.get("name") or "") and "PortraitVisual" not in (v.get("name") or "")]
            cv = fallback = None
            for name, cvid in sorted(cands, key=lambda c: (c[0] != tname, len(c[0]))):
                cvx = self.gd.charvisuals.get(cvid)
                heads = [self.gd.visuals[v].get("name") or "" for sl, v, b in (cvx or {}).get("slots", [])
                         if sl == "Head" and _nz(v) in self.gd.visuals]
                if not heads:
                    continue
                if re.search(r"_Head_[A-Z]$", heads[0]):     # a stock creator head: placeholder unless nothing better
                    fallback = fallback or (cvid, cvx)
                    continue
                cv = (cvid, cvx)
                break
            cv = cv or fallback
            if not cv:
                continue
            entry = self._character_from_cv(label, cv, race_n, sub_n, key, portrait, races, items, "ORIGINS")
            if entry:
                out.append(entry)
        out += self._npc_characters(races, items)
        out += self.creature_characters()
        self._origin_chars = out
        return out

    def _npc_characters(self, races, items):
        out = []
        by_vname = {}
        for vid, v in self.gd.visuals.items():
            by_vname.setdefault(v.get("name"), vid)
        cvs_by_head = {}
        for cid, cvx in self.gd.charvisuals.items():
            for sl, v, b in cvx.get("slots", []):
                if sl == "Head" and _nz(v):
                    cvs_by_head.setdefault(v, []).append((cvx.get("name") or "", cid))
        hair_fix = {"Dame Aylin (Nightsong)": "HAIR_HUM_M_Straight_Long_C_Spring"}     # her own (cinematic, no helmet)
        for label, head, race_n, sub_n, key, wings in self.NPC_CHARACTERS:
            hv = by_vname.get(head)
            if not hv:                                  # named by character visual instead (Yenna)
                cid = next((k for k, v in self.gd.charvisuals.items() if (v.get("name") or "").startswith(head + "_")), None)
                if cid:
                    entry = self._character_from_cv(label, (cid, self.gd.charvisuals[cid]), race_n, sub_n, key, None,
                                                     races, items, "NPCS")
                    if entry:
                        out.append(entry)
                continue
            def has_hair(cid):
                return any(sl == "Hair" and _nz(v) for sl, v, b in self.gd.charvisuals[cid].get("slots", []))
            cands = sorted((c for c in cvs_by_head.get(hv, []) if not self._NPC_CV_SKIP.search(c[0])),
                           key=lambda c: (not has_hair(c[1]), c[0].startswith(("S_", "HUM_", "ELF_", "HEL_", "TIF_", "GNO_")),
                                          len(c[0])))
            pick = {"Dame Aylin (Nightsong)": "S_GLO_Nightsong"}.get(label)   # her free, in-world look (not the prison one)
            if pick:
                cands = sorted(cands, key=lambda c: not c[0].startswith(pick))
            if cands:
                cv = (cands[0][1], self.gd.charvisuals[cands[0][1]])
            else:                                   # no character visual: the head on a stock body
                cv = (None, {"slots": [("Head", hv, "")]})
            entry = self._character_from_cv(label, cv, race_n, sub_n, key, None, races, items, "NPCS")
            if not entry:
                continue
            if label in hair_fix and by_vname.get(hair_fix[label]):
                hv2 = by_vname[hair_fix[label]]
                entry["selection"]["slots"]["Hair"] = next(
                    (o["uuid"] for o in self.origin_visuals().get("Hair", []) if o["visual"] == hv2), "origin:" + hv2)
            if wings and by_vname.get(wings):
                entry["selection"]["slots"]["Wings"] = next(
                    (o["uuid"] for o in self.origin_visuals().get("Wings", []) if o["visual"] == by_vname[wings]),
                    "origin:" + by_vname[wings])
            out.append(entry)
        return out

    def _character_from_cv(self, label, cv, race_n, sub_n, key, portrait, races, items, group):
        if True:
            cvid, cvx = cv
            sel_slots, body = {}, None
            listed = {}                 # same part already offered in the creator lists (by visual name)
            for sl, opts in self.origin_visuals().items():
                for o in opts:
                    listed.setdefault((sl, self.gd.visuals.get(o["visual"], {}).get("name")), o["uuid"])
            for sl, v, b in cvx.get("slots", []):
                if sl in ("Head", "Hair", "Horns", "Beard", "Tail", "Wings", "DragonbornTop", "DragonbornJaw",
                          "DragonbornChin") and _nz(v) in self.gd.visuals:
                    sel_slots[sl] = listed.get((sl, self.gd.visuals[v].get("name")), "origin:" + v)
                    if sl == "Head":
                        hn = self.gd.visuals[v].get("name") or ""
                        m = re.match(r"^[A-Z]+_(FS|MS|F|M)_", hn)
                        body = self._BODY.get(m.group(1)) if m else ((1, 0) if "_CHD_" in hn else None)
            child = any("_CHD_" in (self.gd.visuals[v].get("name") or "") for sl, v, b in cvx.get("slots", [])
                        if sl == "Head" and _nz(v) in self.gd.visuals)
            r = next((x for x in races if x["name"].lower() == race_n.lower()), None)
            if not r or not body:
                return None
            sub = None
            if r["subraces"]:
                sub = next((x["uuid"] for x in r["subraces"] if sub_n and sub_n.lower() in x["name"].lower()),
                           r["subraces"][0]["uuid"])
            if not any((b["body_type"], b["body_shape"]) == body for b in r["bodies"]):
                body = (r["bodies"][0]["body_type"], r["bodies"][0]["body_shape"]) if r["bodies"] else body
            for sl in ("Hair", "Beard", "Horns", "Tail", "Wings", "DragonbornTop", "DragonbornJaw", "DragonbornChin"):
                sel_slots.setdefault(sl, None)        # what they don't have, they don't get (Minsc is bald)
            # starting outfit: items named after them (UNI_Barbarian_Karlach, ARM_Camp_Body_Karlach, ...)
            rx = re.compile(r"(^|_)%s(_|$)" % re.escape(key), re.I)
            best = {}
            for it in items:
                n = it.get("internal") or ""
                if not rx.search(n) or self._ORIGIN_SKIP.search(n) or it["slot"] in ("Amulet", "Ring"):
                    continue
                rank = (0 if "Starter" in n else 1, 0 if n.startswith("UNI_") else 1, len(n))
                if it["slot"] not in best or rank < best[it["slot"]][0]:
                    best[it["slot"]] = (rank, it["uuid"])
            head_vid = next((v for sl, v, b in cvx.get("slots", []) if sl == "Head"), None)
            sel = {"race": r["uuid"], "subrace": sub, "body_type": body[0], "body_shape": body[1],
                   "slots": sel_slots, "equipment": {sl: v[1] for sl, v in best.items()},
                   "toggles": {"tail": True, "nipple_covers": True, "modesty_leaf": True, "origin_markings": True}}
            if child:
                # children build on the child body: their head/hair/horns plus their own outfit
                sel["child"] = True
                sel["equipment"] = {}
                sel["slots"] = {k: v for k, v in sel_slots.items() if k in ("Head", "Hair", "Horns")}
                if cvid and any(cvid in p["Outfit"] for p in self._child_pool().values()):
                    sel["slots"]["Outfit"] = "outfit:" + cvid
            if cvid:
                sel["origin_cv"] = cvid
            return {"id": cvid or ("npc:" + label), "name": label, "portrait": portrait, "group": group,
                    "head_visual": head_vid, "selection": sel}

    def origin_visuals(self):
        """{"Head": [...], "Hair": [...]} options taken from the origin characters' own visuals."""
        if getattr(self, "_origins", None) is not None:
            return self._origins
        by_name = {}
        for cid, cv in self.gd.charvisuals.items():
            by_name.setdefault(cv.get("name") or "", cv)
        vis_by_name = {}
        out = {"Head": [], "Hair": [], "Horns": [], "Wings": []}
        seen = set()
        for label, prefixes in self.ORIGINS:
            cv = None
            for pre in prefixes:
                cands = sorted(n for n in by_name if n.startswith(pre)
                               and "PortraitVisual" not in n or n == pre + "PortraitVisual")
                cands = [n for n in cands if any(s in ("Head", "Hair") for s, v, b in by_name[n].get("slots", []) if v)]
                if cands:
                    cv = by_name[cands[0]]
                    break
            if not cv:
                continue
            for slot, vid, bone in cv.get("slots", []):
                if slot in out and _nz(vid) and vid not in seen and vid in self.gd.visuals:
                    seen.add(vid)
                    vname = self.gd.visuals[vid].get("name") or ""
                    if slot == "Head" and "_Head_" not in vname:
                        continue
                    import re
                    m = re.match(r"^(?:HAIR_)?([A-Z]{3})_(FS|MS|F|M)_", vname)
                    body = {"F": "Body 1", "M": "Body 2", "FS": "Body 3", "MS": "Body 4"}.get(m.group(2)) if m else None
                    out[slot].append({"uuid": "origin:" + vid, "name": label + (" · " + body if body else ""),
                                      "visual": vid, "source": "origin", "group": "ORIGINS", "internal": vname,
                                      "portrait": re.sub(r"[^a-z]", "", label.lower())})
        if self.ORIGIN_EXTRA_HAIR:
            for v in self.gd.visuals.values():
                vis_by_name.setdefault(v.get("name"), v)
            for vid, v in self.gd.visuals.items():
                for label, vname in self.ORIGIN_EXTRA_HAIR:
                    if v.get("name") == vname and vid not in seen:
                        seen.add(vid)
                        out["Hair"].append({"uuid": "origin:" + vid, "name": label, "visual": vid,
                                            "source": "origin", "group": "ORIGINS", "internal": vname,
                                            "portrait": re.sub(r"[^a-z]", "", label.split("(")[0].lower())})
        # cambions, devils and named NPCs: (visual name, label, portrait token or None)
        by_vname = {}
        for vid, v in self.gd.visuals.items():
            by_vname.setdefault(v.get("name"), vid)

        def body_of(n):
            if n.startswith("CAMBION_"):
                return "cambion"
            m2 = re.match(r"^[A-Z]+_(FS|MS|F|M)_", n)
            return bodies.get(m2.group(1)) if m2 else None
        bodies = {"F": "Body 1", "M": "Body 2", "FS": "Body 3", "MS": "Body 4"}
        for slot, table in (("Head", self.NPC_HEADS), ("Hair", self.NPC_HAIR), ("Wings", self.WINGS)):
            for vname, label, who in table:
                vid = by_vname.get(vname)
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                b = body_of(vname)
                if slot == "Wings":     # wings are refitted onto any body, so no body tag
                    out[slot].append({"uuid": "origin:" + vid, "name": label, "visual": vid, "source": "origin",
                                      "group": "WINGS", "internal": vname, "portrait": who})
                    continue
                out[slot].append({"uuid": "origin:" + vid, "name": label + (" · " + b if b else ""), "visual": vid,
                                  "source": "origin", "group": "CAMBIONS & DEVILS", "internal": vname, "portrait": who})
        # unique horns that the creator doesn't offer: Karlach's, broken ones, NPC uniques, a devil's
        bodies = {"F": "Body 1", "M": "Body 2", "FS": "Body 3", "MS": "Body 4"}
        have = {d.get("VisualResource") for k in ("CharacterCreationAppearanceVisuals", "CharacterCreationSharedVisuals")
                for d in self.cc.get(k, {}).values()}
        extra = []
        for vid, v in self.gd.visuals.items():
            n = v.get("name") or ""
            m = re.match(r"^TIF_(FS|MS|F|M)_NKD_Horns_(.+)$", n)
            cm = re.match(r"^CAMBION_(F|M)_NKD_(?:Head_A_)?Horns(?:_(.+))?$", n)
            if (m or cm or n == "ORTHON_Horns_A") and vid not in have and vid not in seen and "_LOD" not in n:
                if cm:
                    label = "Cambion horns " + (cm.group(2) or "").replace("_", " ").strip() + \
                            (" (male)" if cm.group(1) == "M" else " (female)")
                    label = re.sub(r"\s+", " ", label)
                elif m:
                    tag = m.group(2)
                    label = {"Karlach": "Karlach's horns", "A_Broken": "Broken horns"}.get(
                        tag, "Unique " + tag.replace("UNI_", "") if tag.startswith("UNI_") else tag.replace("_", " "))
                    label += " · " + bodies[m.group(1)]
                else:
                    label = "Orthon (devil) horns"
                extra.append({"uuid": "origin:" + vid, "name": label, "visual": vid, "source": "origin",
                              "group": "UNIQUE", "internal": n,
                              "portrait": "karlach" if "Karlach" in n else None})
                seen.add(vid)
        for o in out["Horns"]:                 # horns worn by the origins themselves (Karlach's)
            o["group"] = "UNIQUE"
            o["name"] = o["name"].split(" · ")[0] + "'s horns" + (" · " + o["name"].split(" · ")[1] if " · " in o["name"] else "")
        out["Horns"] = sorted(out["Horns"] + extra, key=lambda o: o["name"])
        self._origins = out
        return out

    def appearance(self, race, subrace, body_type, body_shape, slot, head=None):
        ids = {race, _nz(subrace)}
        out, seen_vis = [], set()
        for d in self.cc.get("CharacterCreationAppearanceVisuals", {}).values():
            if d.get("SlotName") != slot or d.get("RaceUUID") not in ids:
                continue
            if d.get("BodyType", 0) != body_type or d.get("BodyShape", 0) != body_shape:
                continue
            h = _nz(d.get("HeadAppearanceUUID"))
            if h and head and h != head:
                continue
            seen_vis.add(d.get("VisualResource"))
            out.append({"uuid": d["UUID"], "name": self.gd.text(d.get("DisplayName")) or d.get("UUID"),
                        "visual": d.get("VisualResource"), "head": h, "source": "race", "mod": d.get("_mod")})
        shared = self.cc.get("CharacterCreationSharedVisuals", {})
        vids = self.race_list(race, subrace, "Visuals")
        if slot == "Piercing":  # piercings are bone-attached and shared by every race
            vids = [k for k, d in shared.items() if d.get("SlotName") == "Piercing"]
        for vid in vids:
            d = shared.get(vid)
            if not d or d.get("SlotName") != slot or d.get("VisualResource") in seen_vis:
                continue
            out.append({"uuid": d["UUID"], "name": self.gd.text(d.get("DisplayName")) or d.get("UUID"),
                        "visual": d.get("VisualResource"), "bone": d.get("BoneName"), "source": "shared",
                        "mod": d.get("_mod")})
        if slot in ("Head", "Hair", "Horns", "Wings"):
            have = {o.get("visual") for o in out}
            out += [o for o in self.origin_visuals()[slot] if o["visual"] not in have]
        return out

    def colors(self, race, subrace, category):
        table = COLOR_TABLES.get(category)
        out = []
        for cid in self.race_list(race, subrace, category):
            if table:
                d = self.cc.get(table, {}).get(cid)
                if d:
                    out.append({"uuid": cid, "name": self.gd.text(d.get("DisplayName")) or d.get("Name"),
                                "ui": d.get("UIColor"), "preset": d.get("MaterialPresetUUID"), "mod": d.get("_mod")})
            else:
                d = self.cc.get("ColorDefinitions", {}).get(cid)
                if d:
                    out.append({"uuid": cid, "name": self.gd.text(d.get("DisplayName")) or d.get("Name"),
                                "ui": d.get("Color"), "linear": srgb_hex_to_linear(d.get("Color", "#FF000000")),
                                "mod": d.get("_mod")})
        return out

    def materials(self, kind):
        out = []
        for d in self.cc.get("CharacterCreationAppearanceMaterials", {}).values():
            if d.get("MaterialType") == kind:
                out.append({"uuid": d["UUID"], "name": self.gd.text(d.get("DisplayName")) or d.get("Name"),
                            "key": d.get("Name"), "preset": _nz(d.get("MaterialPresetUUID")), "mod": d.get("_mod")})
        return sorted(out, key=lambda x: _natural(x["key"]))

    def material_by_type(self, kind):
        """Single default entries (LipsMakeup, HairHighlight, HairGraying, HornColor, HornTipColor)."""
        for d in self.cc.get("CharacterCreationAppearanceMaterials", {}).values():
            if d.get("MaterialType") == kind:
                return _nz(d.get("MaterialPresetUUID"))
        return None

    # ------------------------------------------------------------ equipment

    def equipment_race(self, root_template):
        return self.gd.template_attr(root_template, "EquipmentRace")

    def _items(self):
        if self._equip_cache is None:
            items = []
            for tid, t in self.gd.templates.items():
                if t.get("Type") != "item":
                    continue
                stats = self.gd.template_attr(tid, "Stats")
                if not stats:
                    continue
                slot = EQUIP_SLOTS.get(self.gd.stat(stats, "Slot") or "")
                if not slot:
                    continue
                eq = self._equipment(tid)
                visual = self.gd.template_attr(tid, "VisualTemplate")
                if not eq and not visual:
                    continue
                name = self.gd.text(self.gd.template_attr(tid, "DisplayName")) or t.get("Name")
                if not name or name.startswith("%%%") or "|" in name:   # placeholder / debug items
                    continue
                items.append({"uuid": tid, "name": name, "internal": t.get("Name"), "slot": slot,
                              "stats": stats, "race_fitted": bool(eq and eq.get("visuals")), "mod": t.get("_mod")})
            items.sort(key=lambda i: (i["slot"], i["name"], i["internal"]))
            self._equip_cache = items
        return self._equip_cache

    def _equipment(self, tid):
        for t in self.gd.template_chain(tid):
            eq = t.get("Equipment")
            if eq and (eq.get("visuals") or eq.get("visual_set")):
                return eq
        return None

    def equipment_race_chain(self, eq_race, item_eq=None):
        """eq_race, then its fallbacks: item-specific ParentRace overrides, else EquipmentRaces.DefaultParent."""
        races = self.cc.get("EquipmentRaces", {})
        chain, cur = [], eq_race
        while cur and cur not in chain:
            chain.append(cur)
            nxt = _nz((item_eq or {}).get("parent_race", {}).get(cur))
            cur = nxt or _nz(races.get(cur, {}).get("DefaultParent"))
        return chain

    def equipment_race_name(self, eq_race):
        return self.cc.get("EquipmentRaces", {}).get(eq_race, {}).get("Name", eq_race)

    def equipment_visuals(self, tid, eq_race, slot=None):
        """Visual resource IDs for an item on a given equipment race (follows fallbacks)."""
        eq = self._equipment(tid)
        if eq and eq.get("visuals"):
            for cur in self.equipment_race_chain(eq_race, eq):
                if cur in eq["visuals"]:
                    return eq["visuals"][cur], eq
            return [], eq
        if slot is not None and slot not in HELD_SLOTS:
            # no worn visual: the VisualTemplate is the model lying on the ground (amulets and rings are
            # never drawn on the body in BG3), so there's nothing to put on the character
            return [], eq
        v = self.gd.template_attr(tid, "VisualTemplate")
        return ([v] if v else []), eq

    def equipment(self, slot=None, eq_race=None):
        out = []
        for it in self._items():
            if slot and it["slot"] != slot:
                continue
            if eq_race and it["race_fitted"]:
                vis, _ = self.equipment_visuals(it["uuid"], eq_race, it["slot"])
                if not vis:
                    continue
            out.append(it)
        return out

    def dyes(self):
        """In-game dye items -> their colour preset (two/three-tone MSK palettes)."""
        out = []
        for tid, t in self.gd.templates.items():
            cp = _nz(t.get("ColorPreset"))
            if not cp or cp not in self.gd.presets or "dye" not in (t.get("Name") or "").lower():
                continue
            name = self.gd.text(self.gd.template_attr(tid, "DisplayName")) or t.get("Name")
            if not name or name.startswith("%%%"):
                continue
            vec = self.gd.presets[cp].get("vec3", {})
            swatch = [srgb_linear_to_hex((vec.get(k) or [[0.5, 0.5, 0.5]])[0])
                      for k in ("Cloth_Primary", "Cloth_Secondary", "Leather_Primary", "Metal_Primary")]
            out.append({"uuid": tid, "name": name, "internal": t.get("Name"), "swatch": swatch, "mod": t.get("_mod"),
                        "icon": self.gd.template_attr(tid, "Icon")})
        out.sort(key=lambda d: d["name"])
        return out

    SCLERA = [("Eye preset", None), ("White", "#F1EEE8"), ("Ivory", "#E9E0CC"), ("Yellowed", "#D9C98F"),
              ("Bloodshot", "#D7A69C"), ("Red", "#8E1A1A"), ("Grey", "#7D7D80"), ("Black", "#0B0A0C")]

    # ------------------------------------------------------------ full menu

    def menu(self, race, subrace, body_type, body_shape, head=None):
        m = {"slots": {}, "colors": {}, "materials": {}}
        for s in APPEARANCE_SLOTS:
            m["slots"][s] = self.appearance(race, subrace, body_type, body_shape, s, head)
        for c in COLOR_CATEGORIES:
            m["colors"][c] = self.colors(race, subrace, c)
        for k in MATERIAL_KINDS:
            m["materials"][k] = self.materials(k)
        m["colors"]["ScleraColors"] = [{"uuid": "sclera:" + (hx or "preset"), "name": n, "ui": hx or "#B9A79E"}
                                       for n, hx in self.SCLERA]
        m["dyes"] = self.dyes()
        m["colors"]["EyeColorsL"] = m["colors"].get("EyeColors", [])
        m["colors"]["ScleraColorsL"] = m["colors"]["ScleraColors"]
        from .recipe import EYE_SHAPES
        m["eye_shapes"] = [{"key": k, "name": v[0], "icon": "tex:" + v[1]} for k, v in EYE_SHAPES.items()]
        return m


def srgb_linear_to_hex(c):
    def f(x):
        x = min(1.0, max(0.0, float(x)))
        return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return "#%02X%02X%02X" % tuple(int(round(f(x) * 255)) for x in c[:3])


def _natural(s):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]
