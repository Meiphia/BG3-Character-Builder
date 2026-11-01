"""Import a Tav's character-creator choices from a BG3 save (.lsv).

A save is an LSPK package; Globals.lsf holds the entity-component state as one binary blob
("NewAge", LSMF format). Rather than decoding the whole ECS format, we recognise the pieces we
need by their GUIDs, which are stored with Larian's usual swapped tail bytes:

  CharacterCreationAppearance   vis_begin, vis_end, el_begin, el_end (u64 pointers into the blob),
                                SkinColor, <vector>, EyeColor, SecondEyeColor, HairColor (GUIDs)
  Elements (48 bytes each)      Material GUID, Color GUID, ColorIntensity, GlossyTint, MetallicTint, pad
  CharacterCreationStats        Race GUID, SubRace GUID, BodyType u8, BodyShape u8

Everything is checked against the game's creator tables, so stray matches are ignored.
"""

import glob
import os
import re
import struct
import uuid

from .lsf import load_resource
from .pak import Pak

NULL = "00000000-0000-0000-0000-000000000000"


def _guid(blob, off):
    b = blob[off:off + 16]
    if len(b) < 16:
        return None
    le = b[:8] + bytes(x for i in range(8, 16, 2) for x in (b[i + 1], b[i]))
    return str(uuid.UUID(bytes_le=le))


def _pattern(u):
    le = uuid.UUID(u).bytes_le
    return le[:8] + bytes(x for i in range(8, 16, 2) for x in (le[i + 1], le[i]))


def save_dirs():
    home = os.path.expanduser("~")
    roots = [os.path.join(home, "Documents", "Larian Studios", "Baldur's Gate 3", "PlayerProfiles")]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(os.path.join(os.environ["LOCALAPPDATA"], "Larian Studios", "Baldur's Gate 3", "PlayerProfiles"))
    return [r for r in roots if os.path.isdir(r)]


def list_saves(limit=200):
    out = []
    for root in save_dirs():
        for path in glob.glob(os.path.join(root, "*", "Savegames", "*", "*", "*.lsv")):
            folder = os.path.basename(os.path.dirname(path))
            m = re.match(r"^(.*?)-\d+__(.*)$", folder)
            out.append({"path": path, "character": m.group(1) if m else folder,
                        "save": m.group(2) if m else os.path.splitext(os.path.basename(path))[0],
                        "mtime": os.path.getmtime(path)})
    out.sort(key=lambda s: -s["mtime"])
    return out[:limit]


EQUIPMENT_SLOTS = ("Helmet", "Body", "Cloak", "Melee main hand", "Melee off hand", "Ranged main hand",
                   "Ranged off hand", "Ring 1", "Underwear", "Boots", "Gloves", "Amulet", "Ring 2", "Wings",
                   "Horns", "Overhead", "Instrument", "Camp body", "Camp shoes")
UI_SLOT = {"Helmet": "Helmet", "Body": "Chest", "Cloak": "Cloak", "Melee main hand": "Main Hand",
           "Melee off hand": "Off Hand", "Ranged main hand": "Ranged", "Underwear": "Underwear", "Boots": "Boots",
           "Gloves": "Gloves", "Camp body": "Camp Clothes", "Camp shoes": "Camp Shoes"}
MEMBER_DATA = struct.Struct("<Qh6x")
HELMET_HIDDEN = (0,)      # PlayerData.HelmetOption values that mean the helmet is hidden


def _g(raw, off=0):
    b = raw[off:off + 16]
    le = b[:8] + bytes(b[8 + (i ^ 1)] for i in range(8))
    return str(uuid.UUID(bytes_le=le))


def read_save(gd, path):
    """-> list of candidate characters [{"label", "selection", "notes", "score", "name"}], best guess first.

    Uses the save's entity-component data (LSMF "NewAge" blob, see lsmf_newage.py):
      game.character_creation.v3.AppearanceComponent  (112 B)  visuals range, material-settings range,
                                                     SkinColor, ?, EyeColor, SecondEyeColor, HairColor
      game.character_creation.v1.AppearanceMaterialSetting (48 B)  Material, Color, ColorIntensity, ...
      game.character_creation.v1.CharacterCreationStatsComponent  Race, SubRace, BodyType, BodyShape, Name
      game.inventory.v0.Wielding/Member + MemberData  equipped items and their equipment slot
      game.item.v0.DyeComponent  dye colour preset on an item
    """
    from .lsmf_newage import NewAge
    pk = Pak(path)
    entry = next((e for e in pk.entries if e.name == "Globals.lsf"), None)
    if entry is None:
        raise ValueError("no Globals.lsf in save")
    res = load_resource(pk.read(entry))
    blob = res.regions["NewAge"].attrs["NewAge"]
    if isinstance(blob, str):
        blob = blob.encode("latin-1")
    na = NewAge(bytes(blob))
    cc = gd.cc
    av, sv = cc.get("CharacterCreationAppearanceVisuals", {}), cc.get("CharacterCreationSharedVisuals", {})
    am, cdef = cc.get("CharacterCreationAppearanceMaterials", {}), cc.get("ColorDefinitions", {})
    races = cc.get("Races", {})

    appearance = na.by_entity("game.character_creation.v3.AppearanceComponent")
    if not appearance:
        appearance = {o: r for name, c in na.components.items() if "AppearanceComponent" in name
                      for o, r in na.by_entity(name).items()}
    stats = na.by_entity("game.character_creation.v1.CharacterCreationStatsComponent")
    custom = set(c.owners or ()) if (c := na.components.get("game.character_creation.v0.IsCustomComponent")) else set()
    party = set(c.owners or ()) if (c := na.components.get("game.party.v0.MemberComponent")) else set()

    # ---- items: entity -> (template, stats) from the Items region; equipment & dyes from the blob
    items = {}
    fac = res.regions.get("Items")
    if fac is not None and fac.children:
        root = fac.children[0]
        creators = root.child("Creators").children if root.child("Creators") is not None else []
        nodes = root.child("Items").children if root.child("Items") is not None else []
        for c, it in zip(creators, nodes):
            ent = na.entity_by_guid.get(_pattern(str(c.attrs.get("Entity"))))
            if ent is not None:
                items[ent] = (str(it.attrs.get("CurrentTemplate") or ""), str(it.attrs.get("Stats") or ""))
    # ---- per-character helmet setting (Characters/CharacterFactory: Creators[i] <-> Characters[i])
    helmet = {}
    fac = res.regions.get("Characters")
    if fac is not None and fac.children:
        root = fac.children[0]
        creators = root.child("Creators").children if root.child("Creators") is not None else []
        nodes = root.child("Characters").children if root.child("Characters") is not None else []
        for c, ch in zip(creators, nodes):
            ent = na.entity_by_guid.get(_pattern(str(c.attrs.get("Entity"))))
            pd = next((x for x in ch.children if x.name == "PlayerData"), None)
            if ent is not None and pd is not None and pd.get("HelmetOption") is not None:
                helmet[ent] = int(pd.get("HelmetOption"))
    cache_parent = {}
    ct = res.regions.get("CacheTemplates")
    if ct is not None:
        for n in ct.walk():
            if n.get("MapKey") and n.get("ParentTemplateId"):
                cache_parent[str(n.get("MapKey"))] = str(n.get("ParentTemplateId"))
    wielder = {}
    for item, raw in na.by_entity("game.inventory.v0.WieldingComponent").items():
        who = na.resolve_ref(struct.unpack_from("<Q", raw)[0])
        if who is not None:
            wielder[item] = who
    slot_of = {}
    for item, raw in na.by_entity("game.inventory.v0.MemberComponent").items():
        try:
            _inv, slot = na.read_struct(MEMBER_DATA, struct.unpack_from("<Q", raw)[0])
            slot_of[item] = slot
        except Exception:
            pass
    dye_of = {}
    for item, raw in na.by_entity("game.item.v0.DyeComponent").items():
        forms = (_g(raw), str(uuid.UUID(bytes_le=bytes(raw[:16]))))
        dye_of[item] = next((f for f in forms if f in gd.presets), forms[0])
    dye_template = {}
    for tid, t in gd.templates.items():
        cp = t.get("ColorPreset")
        if cp and "dye" in (t.get("Name") or "").lower():
            dye_template.setdefault(cp, tid)

    def equipment(ent):
        eq, dyes, names = {}, {}, {}
        for item, who in wielder.items():
            if who != ent or item not in slot_of or item not in items:
                continue
            idx = slot_of[item]
            slot = UI_SLOT.get(EQUIPMENT_SLOTS[idx] if 0 <= idx < len(EQUIPMENT_SLOTS) else "")
            if not slot:
                continue
            tpl = items[item][0]
            seen = set()
            while tpl and tpl not in gd.templates and tpl in cache_parent and tpl not in seen:
                seen.add(tpl)
                tpl = cache_parent[tpl]
            if tpl not in gd.templates:
                names[slot] = items[item][1]
                continue
            eq[slot] = tpl
            d = dye_of.get(item)
            if d and d in gd.presets:
                dyes[slot] = dye_template.get(d, d)
            elif d:
                names["dye:" + slot] = "a dye on " + slot
        return eq, dyes, names

    def name_of(ent):
        raw = stats.get(ent)
        if not raw or len(raw) < 64:
            return None
        try:
            ptr = struct.unpack_from("<Q", raw, 56)[0]
            ln = struct.unpack_from("<I", raw, 64)[0]
            if 0 < ln < 64:
                return na.read(ptr, ln).decode("utf-8", "replace")
        except Exception:
            pass
        return None

    cands = []
    for ent, raw in appearance.items():
        vb, ve, eb, ee = struct.unpack_from("<4Q", raw, 0)
        visuals = []
        try:
            visuals = [_g(na.read(o, 16)) for o in range(vb, ve, 16)] if vb != 0xFFFFFFFFFFFFFFFF else []
        except Exception:
            pass
        st = stats.get(ent)
        race = _g(st, 0) if st else None
        sub = _g(st, 16) if st else None
        sel = {"race": race if race in races else None, "subrace": sub if sub in races else None,
               "body_type": st[32] if st else 1, "body_shape": st[33] if st and st[33] <= 1 else 0,
               "slots": {}, "piercings": [], "colors": {}, "materials": {}, "intensity": {}, "custom_colors": {},
               "equipment": {}, "dyes": {}, "toggles": {}}
        if not sel["race"]:
            continue
        from collections import Counter
        votes = Counter()
        unknown = 0
        unknown_ids = []
        for g in visuals:
            d = av.get(g) or sv.get(g)
            if not d:
                if g != NULL:
                    unknown += 1
                    unknown_ids.append(g)
                continue
            if g in av:
                votes[(int(av[g].get("BodyType", 0)), int(av[g].get("BodyShape", 0)))] += \
                    0.5 if d.get("SlotName") == "Private Parts" else 3.0
            slot = d.get("SlotName")
            if slot == "Piercing":
                sel["piercings"].append(g)
            elif slot:
                sel["slots"][slot] = g
        if votes:
            sel["body_type"], sel["body_shape"] = votes.most_common(1)[0][0]
        for key, off, cat in (("skin", 32, "SkinColors"), ("eye", 64, "EyeColors"), ("hair", 96, "HairColors")):
            g = _g(raw, off)
            if g in cc.get("CharacterCreation" + cat, {}):
                sel["colors"][cat] = g
        eye2 = _g(raw, 80)
        if eye2 != sel["colors"].get("EyeColors") and eye2 in cc.get("CharacterCreationEyeColors", {}):
            sel["toggles"]["heterochromia"] = True
            sel["colors"]["EyeColorsL"] = eye2
        color_cat = {"Tattoo": "TattooColors", "Makeup": "MakeupColors", "HairHighlight": "HairHighlightColors",
                     "HairGraying": "HairGrayingColors", "LipsMakeup": "LipsMakeupColors", "HornColor": "HornColors",
                     "HornTipColor": "HornTipColors"}
        if eb != 0xFFFFFFFFFFFFFFFF:
            for o in range(eb, ee, 48):
                try:
                    rec = na.read(o, 48)
                except Exception:
                    break
                g, col = _g(rec, 0), _g(rec, 16)
                d = am.get(g)
                if not d:
                    continue
                kind = d.get("MaterialType")
                inten = struct.unpack_from("<f", rec, 32)[0]
                if kind in ("Tattoo", "Makeup", "Scar", "Passive"):
                    sel["materials"][kind] = g
                if kind in color_cat and col in cdef:
                    sel["colors"][color_cat[kind]] = col
                if kind in ("Tattoo", "Makeup", "LipsMakeup", "HairHighlight", "HairGraying") and 0 <= inten <= 1.5:
                    sel["intensity"][kind] = round(float(inten), 3)
        eq, dyes, missing = equipment(ent)
        if helmet.get(ent) in HELMET_HIDDEN and "Helmet" in eq:     # "hide helmet" in the game
            sel["hidden_helmet"] = eq.pop("Helmet")
            dyes.pop("Helmet", None)
            missing.pop("dye:Helmet", None)
        # the game shows camp clothes only at camp; keep them out when armour is worn in that slot
        camp = {}
        for armour, campslot in (("Chest", "Camp Clothes"), ("Boots", "Camp Shoes")):
            if armour in eq and campslot in eq:
                camp[campslot] = eq.pop(campslot)
                dyes.pop(campslot, None)
                missing.pop("dye:" + campslot, None)
        sel["camp_equipment"] = camp
        sel["equipment"], sel["dyes"] = eq, dyes
        notes = []
        if unknown:
            notes.append("%d appearance choices weren't found in the game data (newer patch content or a missing mod)" % unknown)
        if sel.get("hidden_helmet"):
            notes.append("helmet hidden in the save, so the Helmet slot is left empty")
        gear_missing = sorted(v for k, v in missing.items() if not k.startswith("dye:"))
        dye_missing = sorted(v for k, v in missing.items() if k.startswith("dye:"))
        if gear_missing:
            notes.append("gear not found in game data: " + ", ".join(gear_missing))
        if dye_missing:
            notes.append("dye colour not found in game data (" + ", ".join(dye_missing) + ")")
        name = name_of(ent)
        score = (2 if ent in custom else 0) + (1 if ent in party else 0)
        cands.append({"score": score, "selection": sel, "notes": notes, "name": name, "unknown": unknown_ids,
                      "party": ent in party,
                      "label": (name + " · " if name else "") + _label(gd, sel)
                               + (" · %d gear" % len(eq) if eq else "")})
    cands.sort(key=lambda c: -c["score"])
    if not cands:
        raise ValueError("no character-creator data found in this save")
    return cands


def _label(gd, sel):
    races = gd.cc.get("Races", {})
    rn = gd.text((races.get(sel.get("subrace") or "") or races.get(sel["race"]) or {}).get("DisplayName")) or "?"
    body = {(1, 0): "Body 1", (0, 0): "Body 2", (1, 1): "Body 3", (0, 1): "Body 4"}.get(
        (sel["body_type"], sel["body_shape"]), "Body ?")
    skin = gd.text((gd.cc.get("CharacterCreationSkinColors", {}).get(sel["colors"].get("SkinColors") or "") or {}).get("DisplayName"))
    return " · ".join(x for x in (rn, body, skin) if x)
