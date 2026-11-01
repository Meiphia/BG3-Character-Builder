"""Turn a creator selection into an assembly recipe: every mesh, material, texture and shader
parameter needed to rebuild the character outside the game.

selection = {
  "race": uuid, "subrace": uuid|None, "body_type": 0|1, "body_shape": 0|1,
  "slots": {"Head": cc_uuid, "Hair": cc_uuid, "Horns": ..., "Beard": ..., "Tail": ..., ...},
  "piercings": [shared_visual_uuid, ...],
  "colors": {"SkinColors": uuid, "EyeColors": uuid, "HairColors": uuid, "HairHighlightColors": uuid,
             "HairGrayingColors": uuid, "MakeupColors": uuid, "LipsMakeupColors": uuid,
             "TattooColors": uuid, "HornColors": uuid, "HornTipColors": uuid},
  "materials": {"Tattoo": cc_uuid, "Makeup": cc_uuid, "Scar": cc_uuid, "Passive": cc_uuid},
  "intensity": {"Tattoo": 1.0, "Makeup": 1.0, "LipsMakeup": 1.0, "HairHighlight": 1.0, "HairGraying": 0.0},
  "equipment": {"Chest": template_uuid, "Helmet": ..., "Main Hand": ..., "Off Hand": ...},
}
"""

import copy

from .creator import Creator, srgb_hex_to_linear, _nz

# Color category -> (shader vec3 params it drives, single-entry AppearanceMaterial type whose preset applies first)
COLOR_PARAMS = {
    "HairHighlightColors": (("Highlight_Color", "Beard_Highlight_Color"), "HairHighlight"),
    "HairGrayingColors": (("Hair_Graying_Color", "Beard_Graying_Color"), "HairGraying"),
    "MakeupColors": (("MakeupColor",), None),
    "LipsMakeupColors": (("Lips_Makeup_Color",), "LipsMakeup"),
    "TattooColors": (("TattooColor", "TattooColorR", "TattooColorG", "TattooColorB", "TattooColorA"), None),
    "HornColors": (("NonSkinColor",), "HornColor"),
    "HornTipColors": (("NonSkinTipColor",), "HornTipColor"),
}
INTENSITY_PARAMS = {
    "Tattoo": ("TattooIntensity",), "Makeup": ("MakeupIntensity",), "LipsMakeup": ("LipsMakeupIntensity",),
    "HairHighlight": ("Highlight_Intensity",), "HairGraying": ("Graying_Intensity", "Beard_Graying_Intensity"),
}

SLOT_ORDER = ("Body", "Head", "Hair", "Beard", "Horns", "Wings", "Tail", "Private Parts",
              "DragonbornTop", "DragonbornJaw", "DragonbornChin")


class RecipeError(Exception):
    pass


def build_recipe(gd, sel, creator=None):
    c = creator or Creator(gd)
    warnings = []
    race, sub = sel["race"], _nz(sel.get("subrace"))
    bt, bs = sel.get("body_type", 1), sel.get("body_shape", 0)

    preset = c.preset_for(race, sub, bt, bs)
    if not preset:
        raise RecipeError("no creator preset for that race/body")
    root = preset["root_template"]
    cv_id = gd.template_attr(root, "CharacterVisualResourceID")
    child = bool(sel.get("child"))
    creature = sel.get("creature_cv") if sel.get("creature_cv") in gd.charvisuals else None
    if creature:                                # a creature/devil/cambion: its own skeleton, body and look
        cv_id = creature
    elif child:                                   # children: the game's own child body for this race
        cv_id = c.child_base(race)
        if not cv_id:
            raise RecipeError("this race has no child body in the game")
    cv = gd.charvisuals.get(cv_id)
    if not cv:
        raise RecipeError("character visual %s not found" % cv_id)
    eq_race = c.equipment_race(root) if not (child or creature) else None

    # ---------------------------------------------------------------- pick visuals per slot
    slots = {}  # slot -> {"visual": id, "bone": str|None, "source": str}
    base = gd.visuals.get(cv.get("base_visual") or "") or {}
    body_vis = _nz(cv.get("body_set_visual")) or (_nz(cv.get("base_visual")) if base.get("source") else None)
    ocv = gd.charvisuals.get(sel.get("origin_cv") or "")      # loaded as an origin character
    if ocv and _nz(ocv.get("body_set_visual")) in gd.visuals and \
            not (gd.visuals[ocv["body_set_visual"]].get("name") or "").startswith("Placeholder"):
        body_vis = _skin_body(gd, ocv["body_set_visual"]) or body_vis   # their own body (scars, tattoos)
    if body_vis:
        slots["Body"] = {"visual": body_vis, "source": "body set"}
    for i, vid in enumerate((sel.get("creature_outfit") or []) if creature else []):
        if vid in gd.visuals:
            slots["Outfit#%d" % (i + 1)] = {"visual": vid, "bone": None, "source": "outfit",
                                            "option": gd.visuals[vid].get("name")}
    if ocv:
        # parts that sit on an origin's body: Karlach's infernal engine plates
        import re as _r
        stem = _r.sub(r"(_Glow(_\d+)?|_FX|_Fire)$", "", (gd.visuals.get(body_vis) or {}).get("name") or "")
        for k, vv in gd.visuals.items():
            if stem and vv.get("name") == stem + "_MetalParts":
                slots["Body#engine"] = {"visual": k, "source": "origin", "option": vv.get("name")}
                break
    keep = ("Head", "Hair", "Horns", "Beard", "Tail", "Wings", "ModestyLeaf", "Private Parts", "Piercing")
    for slot, vis, bone in cv.get("slots", []):
        if creature and sel.get("creature_outfit") and slot not in keep:
            continue                            # a replacement outfit was given (Mizora's own clothes)
        if _nz(vis):
            key = slot
            n = 2
            while key in slots:          # e.g. two ModestyLeaf pieces
                key = "%s#%d" % (slot, n)
                n += 1
            slots[key] = {"visual": vis, "bone": bone or None, "source": "default"}

    av = gd.cc.get("CharacterCreationAppearanceVisuals", {})
    sv = gd.cc.get("CharacterCreationSharedVisuals", {})
    for slot, ccid in (sel.get("slots") or {}).items():
        if ccid is None:
            slots.pop(slot, None)
            continue
        if isinstance(ccid, str) and ccid.startswith("outfit:"):      # a child's outfit: a whole clothing set
            slots.pop(slot, None)
            for i, vid in enumerate(c.child_outfit_visuals(ccid.split(":", 1)[1])):
                slots["Outfit#%d" % (i + 1)] = {"visual": vid, "bone": None, "source": "outfit",
                                                "option": gd.visuals[vid].get("name")}
            continue
        if isinstance(ccid, str) and ccid.startswith("origin:"):      # an origin character's own head / hair
            vid = ccid.split(":", 1)[1]
            if vid in gd.visuals:
                slots[slot] = {"visual": vid, "bone": None, "source": "origin",
                               "option": gd.visuals[vid].get("name")}
            else:
                warnings.append("unknown %s option %s" % (slot, ccid))
            continue
        d = av.get(ccid) or sv.get(ccid)
        if not d:
            warnings.append("unknown %s option %s" % (slot, ccid))
            continue
        if not _nz(d.get("VisualResource")):      # "No Beard" / "No Horns" style options
            slots.pop(slot, None)
            continue
        slots[slot] = {"visual": d.get("VisualResource"), "bone": d.get("BoneName"), "source": "creator",
                       "option": gd.text(d.get("DisplayName"))}

    toggles = sel.get("toggles") or {}
    _apply_body_toggles(gd, slots, toggles)

    attachments = []
    for pid in sel.get("piercings") or []:
        d = sv.get(pid)
        if d:
            attachments.append({"slot": "Piercing", "visual": d.get("VisualResource"),
                                "bone": d.get("BoneName"), "option": gd.text(d.get("DisplayName"))})

    # ---------------------------------------------------------------- character-wide material overrides
    char_ov = _empty_ov()
    _merge_ov(char_ov, cv.get("overrides"), gd)                 # template defaults (default skin/hair/eye presets)
    if ocv:
        # the origin's own skin, hair and eye colours; their visual lists these as plain values (flagged off,
        # unlike a preset's), so take them all
        _apply_preset(char_ov, ocv.get("overrides"), gd, include_disabled=True)

    colors = {k: v for k, v in (sel.get("colors") or {}).items() if v != "origin"}   # "origin": keep their own
    for cat, table in (("SkinColors", "CharacterCreationSkinColors"), ("EyeColors", "CharacterCreationEyeColors"),
                       ("HairColors", "CharacterCreationHairColors")):
        cid = colors.get(cat)
        if cid:
            d = gd.cc.get(table, {}).get(cid)
            if d and _nz(d.get("MaterialPresetUUID")):
                _apply_preset(char_ov, gd.presets.get(d["MaterialPresetUUID"]), gd)
            else:
                warnings.append("unknown %s %s" % (cat, cid))

    for kind, ccid in (sel.get("materials") or {}).items():
        d = gd.cc.get("CharacterCreationAppearanceMaterials", {}).get(ccid)
        if not d:
            warnings.append("unknown %s %s" % (kind, ccid))
            continue
        if _nz(d.get("MaterialPresetUUID")):
            _apply_preset(char_ov, gd.presets.get(d["MaterialPresetUUID"]), gd)
        else:  # "None" choices zero the corresponding index
            for p in {"Tattoo": ("TattooIndex",), "Makeup": ("MakeUpIndex",), "Scar": ("ScarIndex", "Scar_Weight")}.get(kind, ()):
                char_ov["scalar"][p] = 0.0

    for cat, (params, base_type) in COLOR_PARAMS.items():
        cid = colors.get(cat)
        if not cid:
            continue
        if base_type:
            _apply_preset(char_ov, gd.presets.get(c.material_by_type(base_type)), gd)
        d = gd.cc.get("ColorDefinitions", {}).get(cid)
        if not d:
            warnings.append("unknown %s %s" % (cat, cid))
            continue
        rgb = srgb_hex_to_linear(d.get("Color", "#FF000000"))
        for p in params:
            char_ov["vec3"][p] = rgb

    _apply_custom_colors(char_ov, sel.get("custom_colors") or {})

    for kind, val in (sel.get("intensity") or {}).items():
        for p in INTENSITY_PARAMS.get(kind, ()):
            if p == "TattooIntensity":
                # keep the preset's channel choice (which component is non-zero), scale its strength
                cur = char_ov["vec4"].get(p) or [1.0, 0.0, 0.0, 0.0]
                peak = max(cur) or 1.0
                char_ov["vec4"][p] = [c / peak * val for c in cur]
            else:
                char_ov["scalar"][p] = float(val)

    sclera = colors.get("ScleraColors") or ""
    if sclera.startswith("sclera:#"):
        _apply_custom_colors(char_ov, {"ScleraColors": sclera.split(":", 1)[1]})
    if (sel.get("custom_colors") or {}).get("ScleraColors"):
        _apply_custom_colors(char_ov, {"ScleraColors": sel["custom_colors"]["ScleraColors"]})

    if toggles.get("glowing_iris"):
        for k in ("GlowBrightness", "GlowBrightness_L"):
            char_ov["scalar"][k] = 10.0

    illithid = None
    if toggles.get("ceremorphosis"):
        dgb = "dragonborn" in (c.race_name(race) or "").lower()
        illithid = ILLITHID["dgb" if dgb else "shared"]
        for name in (illithid["skin_preset"] if not dgb else illithid["skin_preset"][bt == 0],
                     ILLITHID["eye_preset"]):
            pid = _preset_by_name(gd, name)
            if pid:
                _apply_preset(char_ov, gd.presets[pid], gd)
        char_ov["scalar"]["IllithidColorWeight"] = 1.0

    # ---------------------------------------------------------------- equipment
    equipment = []
    for eslot, tid in (sel.get("equipment") or {}).items():
        if not tid or child or creature:        # gear models are made for the playable bodies only
            continue
        vis, eq = c.equipment_visuals(tid, eq_race, eslot.split("#")[0])
        if not vis and eslot in ("Amulet", "Ring", "Ring 1", "Ring 2"):
            continue                    # jewelry has no worn model in BG3: stats only
        if not vis:
            warnings.append("%s %s has no visual for %s" % (eslot, tid, c.equipment_race_name(eq_race)))
            continue
        item_ov = _empty_ov()
        if eq and eq.get("visual_set"):
            _merge_ov(item_ov, eq["visual_set"], gd)
        dye = (sel.get("dyes") or {}).get(eslot)
        if dye:
            cp = _nz((gd.templates.get(dye) or {}).get("ColorPreset"))
            if cp and cp in gd.presets:
                _apply_preset(item_ov, gd.presets[cp], gd)
            else:
                warnings.append("dye %s has no colour preset" % dye)
        name = gd.text(gd.template_attr(tid, "DisplayName")) or gd.templates.get(tid, {}).get("Name")
        sheath = _sheath_bone(gd, tid, eslot, sel.get("weapons") or "melee")
        for v in vis:
            equipment.append({"slot": eslot, "item": tid, "item_name": name, "visual": v, "overrides": item_ov,
                              "sheath": sheath})

    # ---------------------------------------------------------------- resolve everything
    out = {
        "format": "bg3asm-recipe/1",
        "cloth_bones": bool((sel.get("toggles") or {}).get("cloth_physics", True)),
        "breast_bones": (not child) and bool((sel.get("toggles") or {}).get("chest_physics", bt == 1)),
        "part_scale": {k: float(v) for k, v in (sel.get("part_scale") or {}).items()
                       if k in ("Horns", "Wings") and isinstance(v, (int, float))},
        "character": {
            "race": c.race_name(race), "subrace": c.race_name(sub) if sub else None,
            "body_type": bt, "body_shape": bs, "root_template": root,
            "character_visual": cv_id, "equipment_race": c.equipment_race_name(eq_race),
        },
        "skeleton": None,
        "parts": [],
        "materials": {},
        "textures": {},
        "virtual_textures": {},
        "warnings": warnings,
        "body_mask_codes": dict(gd.body_mask_codes),
    }

    def add_visual(entry, overrides, kind):
        vid = entry["visual"]
        v = gd.visuals.get(vid)
        if not v:
            warnings.append("visual %s (%s) not found" % (vid, entry.get("slot")))
            return
        gr2 = gd.locate(v["source"]) if v["source"] else None
        part = {
            "kind": kind, "slot": entry.get("slot"), "sheath": entry.get("sheath"), "option": entry.get("option") or entry.get("item_name"),
            "visual_id": vid, "name": v["name"],
            "gr2": {"path": gr2[1], "pak": gr2[0]} if gr2 else {"path": v["source"], "pak": None},
            "attach_bone": entry.get("bone") or _nz(v.get("attach_bone")),
            "visual_slot": v.get("slot"),
            "remap_reference": _remap_reference(gd, v),
            "remap_scale": _remap_scale(gd, v, ("F" if bt == 1 else "M") + ("S" if bs == 1 else "")),
            "supports_mask": v.get("supports_mask", False),
            "mask_slots": v.get("mask_slots", []),
            "objects": [],
        }
        if not gr2:
            warnings.append("mesh file missing: %s" % v["source"])
        known = [m for _o, m, _l in v["objects"] if m in gd.materials]
        for obj, mid, lod in v["objects"]:
            if lod not in (0, None):
                continue
            if "_Scalp_" in (obj or ""):
                if not entry.get("scalp_material"):
                    continue            # bald: no scalp layer
                mid = entry["scalp_material"]
            if "Eyeshadow_Mesh" in (obj or ""):
                continue                # baked lash shadow card; no use outside the game renderer
            if "Eyelashes_Mesh" in (obj or "") and "Dithered" in ((gd.materials.get(mid) or {}).get("source") or ""):
                # some unique heads (Aylin's) put the soft dithered shadow material on their lashes, which only
                # makes sense with the game's TAA; use the regular lash material instead
                std = _material_named(gd, "SHARED_LashShadow")
                if std:
                    mid = std
            if mid not in gd.materials and known:
                warnings.append("material %s missing; using sibling %s for %s" % (mid, known[0], obj))
                mid = known[0]
            key = mid
            if mid and kind == "equipment":   # items can tint a shared material differently
                key = "%s@%s" % (mid, entry.get("item"))
            part["objects"].append({"object": obj, "mesh": obj.split(".")[1] if obj and "." in obj else obj,
                                    "material": key})
            if mid and key not in out["materials"]:
                mat = resolve_material(gd, mid, overrides, out, warnings)
                if mat:
                    out["materials"][key] = mat
        out["parts"].append(part)
        if out["skeleton"] is None and kind == "body":
            sk = gd.skeletons.get(base.get("skeleton") or "") or gd.skeletons.get(v.get("skeleton") or "")
            if sk:
                loc = gd.locate(sk["source"])
                out["skeleton"] = {"name": sk["name"], "path": loc[1] if loc else sk["source"],
                                   "pak": loc[0] if loc else None}

    # the hairstyle paints its hairline / short hair onto the head's scalp mesh
    hair_vis = gd.visuals.get((slots.get("Hair") or {}).get("visual") or "") or {}
    scalp_mat = hair_vis.get("scalp_material") if hair_vis.get("scalp_material") in gd.materials else None
    if not scalp_mat and hair_vis.get("name"):
        # some hair (Karlach's) doesn't name its scalp layer; the material is "<hair visual>_Scalp"
        want = hair_vis["name"] + "_Scalp"
        by_name = getattr(gd, "_mat_by_name", None)
        if by_name is None:
            by_name = gd._mat_by_name = {}
            for k, m in gd.materials.items():
                by_name.setdefault(m.get("name"), k)
        scalp_mat = by_name.get(want)

    for slot in sorted(slots, key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else 99):
        e = dict(slots[slot], slot=slot.split("#")[0])
        ov = char_ov
        if e["slot"] == "Head":
            e["scalp_material"] = scalp_mat
            if e.get("source") == "origin" and toggles.get("origin_markings"):
                ov = _empty_ov()        # the head's own tattoo atlas and index win over the creator pick
                _merge_ov(ov, char_ov, gd)
                ov["scalar"].pop("TattooIndex", None)
            elif e.get("source") == "origin":
                ov = _origin_head_ov(gd, c, char_ov, race, sub, bt, bs)
        add_visual(e, ov, "body" if slot == "Body" else "appearance")
    for a in attachments:
        add_visual(a, char_ov, "attachment")
    for e in equipment:
        ov = _empty_ov()
        _merge_ov(ov, char_ov, gd)     # skin params still apply to skin bits of armor
        _merge_ov(ov, e["overrides"], gd)
        add_visual(e, ov, "equipment")

    if illithid:
        _add_illithid_masks(gd, out, illithid)
    if sel.get("eye_shape") in EYE_SHAPES:
        rn, cn = EYE_SHAPES[sel["eye_shape"]][1:]
        for mat in out["materials"].values():
            if mat.get("shader_family") == "eye":
                for slot, name in (("RegionMask", rn), ("ColorsMask", cn)):
                    tid = _by_name(gd, "textures", name)
                    if tid:
                        _register_texture(gd, out, tid)
                        mat["textures"][slot] = tid
    if toggles.get("heterochromia"):
        left_ov = _empty_ov()
        cid = colors.get("EyeColorsL")
        d = gd.cc.get("CharacterCreationEyeColors", {}).get(cid or "")
        if d and _nz(d.get("MaterialPresetUUID")):
            _apply_preset(left_ov, gd.presets.get(d["MaterialPresetUUID"]), gd)
        custom_l = (sel.get("custom_colors") or {}).get("EyeColorsL")
        if custom_l:
            _apply_custom_colors(left_ov, {"EyeColors": custom_l})
        scl = colors.get("ScleraColorsL") or ""
        if scl.startswith("sclera:#"):
            _apply_custom_colors(left_ov, {"ScleraColors": scl.split(":", 1)[1]})
        if (sel.get("custom_colors") or {}).get("ScleraColorsL"):
            _apply_custom_colors(left_ov, {"ScleraColors": sel["custom_colors"]["ScleraColorsL"]})
        for key in [k for k, m in out["materials"].items() if m.get("shader_family") == "eye"]:
            lm = copy.deepcopy(out["materials"][key])
            for p in ("Eyes_IrisColour", "Eyes_IrisSecondaryColour", "Eyes_GlowColour", "Eyes_ScleraColour"):
                if p in left_ov["vec3"]:
                    lm["vec3"][p] = left_ov["vec3"][p]
            if "Redness" in left_ov["scalar"]:
                lm["scalar"]["Redness"] = left_ov["scalar"]["Redness"]
            lm["name"] = (lm.get("name") or "EYES") + "_Left"
            out["materials"][key + "#L"] = lm
            for part in out["parts"]:
                for o in part["objects"]:
                    if o.get("material") == key:
                        o["material_left"] = key + "#L"
    _apply_weathering(gd, out, sel.get("weathering") or {})
    for mat in out["materials"].values():            # wet tearline strip along the lids
        if "Tearline" in (mat.get("shader") or "") and "Alpha" not in mat["textures"]:
            tid = _by_name(gd, "textures", "EYES_Tearline_MSK1")
            if tid:
                _register_texture(gd, out, tid)
                mat["textures"]["Alpha"] = tid
    for mat in out["materials"].values():
        if mat.get("shader_family") == "eye":
            mat["scalar"]["BG_EyeVeins"] = float((sel.get("weathering") or {}).get("EyeVeins", 0.3))
    if toggles.get("glowing_iris"):          # plain (non-FX) eye materials have no glow parameters
        for mat in out["materials"].values():
            if mat.get("shader_family") == "eye":
                mat["scalar"]["GlowBrightness"] = 10.0
    return out


# -------------------------------------------------------------------- body toggles / ceremorphosis

ILLITHID = {
    "shared": {"skin_preset": "Half_Illithid_A", "head": "VFX_BC4_Shared_Half_Illithid_Head",
               "body": "VFX_BC4_Shared_Half_Illithid_Body"},
    "dgb": {"skin_preset": ("DGB_F_Half_Illithid_A", "DGB_M_Half_Illithid_A"),
            "head": "VFX_BC4_DGB_Half_Illithid_Head", "body": "VFX_BC4_DGB_Half_Illithid_Body_01"},
    "eye_preset": "EYES_Half_Illithid",
}


def _sheath_bone(gd, tid, slot, mode):
    """Where a weapon goes when it isn't in hand (the skeleton's Dummy_Sheath_* points), or None if it
    stays in hand. mode: "melee" (melee drawn, ranged on the back), "ranged" (bow drawn), "sheathed"."""
    if slot not in ("Main Hand", "Off Hand", "Ranged"):
        return None
    drawn = {"melee": ("Main Hand", "Off Hand"), "ranged": ("Ranged",), "sheathed": ()}.get(mode, ("Main Hand", "Off Hand"))
    if slot in drawn:
        return None
    name = (gd.templates.get(tid) or {}).get("Name") or ""
    stats = gd.template_attr(tid, "Stats")
    props = str(gd.stat(stats, "Weapon Properties") or "") if stats else ""
    if slot == "Ranged":
        return "Dummy_Sheath_Ranged"
    if slot == "Off Hand":
        return "Dummy_Sheath_Shield" if "shield" in name.lower() or (stats and "shield" in stats.lower()) else "Dummy_Sheath_Hip_R"
    if "Twohanded" in props or "Heavy" in props:
        return "Dummy_Sheath_Upper_R"
    return "Dummy_Sheath_Hip_L"


def _by_name(gd, table, name):
    idx = getattr(gd, "_name_idx_" + table, None)
    if idx is None:
        idx = {}
        for k, v in getattr(gd, table).items():
            idx.setdefault(v.get("name"), k)
        setattr(gd, "_name_idx_" + table, idx)
    return idx.get(name)


def _preset_by_name(gd, name):
    return _by_name(gd, "presets", name)


def _apply_body_toggles(gd, slots, toggles):
    """Modesty pieces on/off, and the tiefling tail (the player character visuals leave it out)."""
    def vname(key):
        return (gd.visuals.get(slots[key].get("visual") or "") or {}).get("name") or ""
    for key in list(slots):
        n = vname(key)
        if toggles.get("nipple_covers") is False and "NippleCover" in n:
            slots.pop(key)
        elif toggles.get("modesty_leaf") is False and "_Leaf_" in n:
            slots.pop(key)
    body = vname("Body") if "Body" in slots else ""
    import re
    m = re.match(r"(TIF_(?:FS|MS|F|M|CHD))_NKD_Body", body)
    if m and "Tail" not in slots and toggles.get("tail", True):
        vid = _by_name(gd, "visuals", m.group(1) + "_NKD_Tail_A") or _by_name(gd, "visuals", m.group(1) + "_NKD_Body_A_Tail")
        if vid:
            slots["Tail"] = {"visual": vid, "bone": None, "source": "tail"}


WEATHERING = {"Freckles": "BG_Freckles", "Vitiligo": "BG_Vitiligo", "Age": "BG_Age", "BodyHair": "BG_BodyHair",
              "Dirt": "Dirt", "Blood": "Blood", "Sweat": "Sweat", "Bruises": "Bruises"}
# shared skin masks (all heads share one UV layout, as do the bodies)
SHARED_SKIN_MAPS = {
    "skin_head": {"BG_GenericMaps": "SKIN_Shared_Shading_MSKA", "BG_DirtMaps": "SKIN_Shared_Dirt_Head_MSKA",
                  "BG_SweatMaps": "SKIN_Shared_Sweat_MSK"},
    "skin_body": {"BG_GenericMaps": "SKIN_Shared_Shading_MSK", "BG_DirtMaps": "SKIN_Shared_Dirt_Body_MSK",
                  "BG_SweatMaps": "SKIN_Shared_Body_Sweat_MSK"},
}


def _apply_weathering(gd, out, w):
    for mat in out["materials"].values():
        fam = mat.get("shader_family")
        name = mat.get("name") or ""
        if fam not in SHARED_SKIN_MAPS or "Nipple" in name or "Tail" in name:
            continue
        for slot, tname in SHARED_SKIN_MAPS[fam].items():
            tid = _by_name(gd, "textures", tname)
            if tid:
                _register_texture(gd, out, tid)
                mat["textures"][slot] = tid
        for k, p in WEATHERING.items():
            if k in w:
                mat["scalar"][p] = float(w[k])


EYE_SHAPES = {
    "round": ("Round", "EYES_AO_Region_MSKA", "EYES_Parts_MSKA"),
    "cat": ("Cat slit", "EYES_AO_Cat_Region_MSKA", "EYES_Cat_Parts_MSKA"),
    "goat": ("Goat", "EYES_AO_Goat_Region_MSKA", "EYES_Goat_Parts_MSKA"),
    "pinpoint": ("Pinpoint", "EYES_AO_Pinpoint_Region_MSKA", "EYES_Pinpoint_Parts_MSKA"),
    "gith": ("Githyanki A", "EYES_AO_Gith_Region_MSKA", "EYES_Gith_Parts_MSKA"),
    "gith_b": ("Githyanki B", "EYES_AO_Gith_Region_B_MSKA", "EYES_Gith_Parts_B_MSKA"),
    "gith_c": ("Githyanki C", "EYES_AO_Gith_Region_C_MSKA", "EYES_Gith_Parts_C_MSKA"),
    "gith_d": ("Githyanki D", "EYES_AO_Gith_Region_D_MSKA", "EYES_Gith_Parts_D_MSKA"),
    "gith_e": ("Githyanki E", "EYES_AO_Gith_Region_E_MSKA", "EYES_Gith_Parts_E_MSKA"),
    "dragon": ("Dragonborn", "EYES_AO_DGB_Region", "EYES_DGB_Parts"),
}


def _register_texture(gd, out, tid):
    t = gd.textures.get(tid)
    if t and tid not in out["textures"]:
        loc = gd.locate(t["source"])
        out["textures"][tid] = {"name": t["name"], "path": loc[1] if loc else t["source"],
                                "pak": loc[0] if loc else None, "srgb": t["srgb"], "size": t["size"]}


def _add_illithid_masks(gd, out, cfg):
    for mat in out["materials"].values():
        fam = mat.get("shader_family")
        if fam not in ("skin_head", "skin_body") or "Nipple" in (mat.get("name") or ""):
            continue
        tid = _by_name(gd, "textures", cfg["head" if fam == "skin_head" else "body"])
        t = gd.textures.get(tid or "")
        if not t:
            continue
        mat["textures"]["IllithidMSK"] = tid
        mat["scalar"]["IllithidColorWeight"] = 1.0
        if tid not in out["textures"]:
            loc = gd.locate(t["source"])
            out["textures"][tid] = {"name": t["name"], "path": loc[1] if loc else t["source"],
                                    "pak": loc[0] if loc else None, "srgb": t["srgb"], "size": t["size"]}


# -------------------------------------------------------------------- materials

def _origin_head_ov(gd, c, char_ov, race, sub, bt, bs):
    """Origin heads carry their own tattoo atlas and index (Minthara's markings, say). Unless asked to
    keep them, give the head the creator's tattoo atlas for this race/body so the character's own
    tattoo choice applies, and no tattoo when none was chosen."""
    ov = _empty_ov()
    _merge_ov(ov, char_ov, gd)
    atlas = None
    for o in c.appearance(race, sub, bt, bs, "Head"):
        if o.get("source") == "origin":
            continue
        v = gd.visuals.get(o.get("visual") or "") or {}
        for _obj, mid, lod in v.get("objects", []):
            t = ((gd.materials.get(mid) or {}).get("tex") or {}).get("TattooAtlas")
            if t:
                atlas = t[0]
                break
        if atlas:
            break
    if atlas:
        ov["tex"]["TattooAtlas"] = atlas
    if "TattooIndex" not in ov["scalar"]:
        ov["scalar"]["TattooIndex"] = 0.0
    return ov


def _empty_ov():
    return {"scalar": {}, "vec3": {}, "vec4": {}, "tex": {}, "vt": {}}


def _material_named(gd, name):
    by_name = getattr(gd, "_mat_by_name", None)
    if by_name is None:
        by_name = gd._mat_by_name = {}
        for k, m in gd.materials.items():
            by_name.setdefault(m.get("name"), k)
    return by_name.get(name)


def _skin_body(gd, vid):
    """An origin's body set can be an effect layer (Karlach's fire body, Gale's god body uses a plain SSS shader
    which is fine). When its material isn't a skin shader, use the same body without the effect suffix."""
    v = gd.visuals.get(vid) or {}

    def skin(vv):
        return any(shader_family((gd.materials.get(m) or {}).get("source")) in ("skin_body", "base")
                   for _o, m, lod in vv.get("objects", []) if not lod)
    if skin(v):
        return vid
    import re as _r
    base = _r.sub(r"(_Glow(_\d+)?|_FX|_Fire)$", "", v.get("name") or "")
    for k, vv in gd.visuals.items():
        if vv.get("name") == base and skin(vv):
            return k
    return None


def _apply_preset(ov, preset, gd, depth=0, include_disabled=False):
    if not preset or depth > 4:
        return
    for sub in (preset.get("presets") or {}).values():
        _apply_preset(ov, gd.presets.get(sub), gd, depth + 1)
    if _nz(preset.get("color_preset")):
        _apply_preset(ov, gd.presets.get(preset["color_preset"]), gd, depth + 1)
    for k in ("scalar", "vec3", "vec4", "tex", "vt"):
        for name, val in (preset.get(k) or {}).items():
            if isinstance(val, tuple):
                val, enabled = val
                if enabled is False and not include_disabled:
                    continue
            ov[k][name] = val


def _merge_ov(dst, src, gd):
    if not src:
        return
    if "presets" in src or "color_preset" in src:
        _apply_preset(dst, src, gd)
        return
    for k in ("scalar", "vec3", "vec4", "tex", "vt"):
        dst[k].update(src.get(k) or {})


def resolve_material(gd, mid, ov, out, warnings):
    m = gd.materials.get(mid)
    if not m:
        warnings.append("material %s not found" % mid)
        return None
    res = {"name": m["name"], "shader": m["source"], "shader_family": shader_family(m["source"]),
           "scalar": {}, "vec3": {}, "vec4": {}, "textures": {}, "virtual_textures": {}}
    for k, dst in (("scalar", "scalar"), ("vec3", "vec3"), ("vec4", "vec4")):
        for name, (val, _en) in m[k].items():
            res[dst][name] = ov[k].get(name, val)
    for name, (tid, _en) in m["tex"].items():
        tid = ov["tex"].get(name, tid)
        if not _nz(tid):
            continue
        t = gd.textures.get(tid)
        if not t:
            continue
        res["textures"][name] = tid
        if tid not in out["textures"]:
            loc = gd.locate(t["source"])
            out["textures"][tid] = {"name": t["name"], "path": loc[1] if loc else t["source"],
                                    "pak": loc[0] if loc else None, "srgb": t["srgb"], "size": t["size"]}
            if not loc:
                warnings.append("texture file missing: %s" % t["source"])
    for name, (vid, _en) in m["vt"].items():
        vid = ov["vt"].get(name, vid)
        vt = gd.vtextures.get(vid or "")
        if vt:
            res["virtual_textures"][name] = vid
            out["virtual_textures"].setdefault(vid, {"name": vt["name"], "gtex": vt["gtex"]})
    return res


def shader_family(src):
    s = (src or "").rsplit("/", 1)[-1].lower()
    if "vfx_cinematic_char_sss" in s:          # skin with a heat/fire layer (Karlach's body)
        return "skin_body"
    for key, fam in (("skin_head", "skin_head"), ("skin_body", "skin_body"), ("scalp", "scalp"),
                     ("hair", "hair"), ("eye", "eye"), ("horns", "horns"), ("fur", "fur"),
                     ("char_base", "base"), ("weapon", "base"), ("_base", "base")):
        if key in s:
            return fam
    return "other"


_REMAP_BASES = {"F": "Humans/_Female/HUM_F_Base", "M": "Humans/_Male/HUM_M_Base",
                "FS": "Humans/_FemaleStrong/HUM_FS_Base", "MS": "Humans/_MaleStrong/HUM_MS_Base"}


def _head_size(gd, key):
    """(height, depth) of a stock human head for body key F/M/FS/MS, from visual bounds."""
    cache = getattr(gd, "_head_sizes", None)
    if cache is None:
        cache = gd._head_sizes = {}
    if key not in cache:
        size = None
        for v in gd.visuals.values():
            n = v.get("name") or ""
            if n.startswith("HUM_%s_NKD_Head_" % key) and "Remap" not in n and v.get("bounds") and v["bounds"][0]:
                lo, hi = v["bounds"]
                size = (hi[1] - lo[1], hi[2] - lo[2])
                break
        cache[key] = size
    return cache[key]


def _remap_scale(gd, v, char_key):
    """Head-size ratio between the character's body and the body a remappable part was made for."""
    if not v.get("needs_remap"):
        return 1.0
    import re
    m = re.search(r"_(FS|MS|F|M)_", v.get("name") or "")
    if not m or m.group(1) == char_key:
        return 1.0
    a, b = _head_size(gd, char_key), _head_size(gd, m.group(1))
    if not a or not b:
        return 1.0
    return round(0.5 * (a[0] / b[0] + a[1] / b[1]), 4)


def _remap_reference(gd, v):
    """Skeleton a remappable visual (hair) was authored against, guessed from its name:
    HAIR_HUM_F_* -> human female base, *_M_* -> male, FS/MS -> strong bodies."""
    if not v.get("needs_remap"):
        return None
    import re
    m = re.search(r"_(FS|MS|F|M)_", v.get("name") or "")
    if not m:
        return None
    loc = gd.locate("Public/Shared/Assets/Characters/_Anims/%s.GR2" % _REMAP_BASES[m.group(1)])
    return {"pak": loc[0], "path": loc[1]} if loc else None


# ---------------------------------------------------------------- custom colours (hue sliders / picker)

CUSTOM_PARAMS = {
    "HairColors": ("Hair_Color", "Beard_Color", "Eyebrow_Color", "Body_Hair_Color"),
    "EyeColors": ("Eyes_IrisColour",),
}


def _apply_custom_colors(ov, custom):
    """custom: {category: "#rrggbb"} -> shader parameters. Skin inverts Larian's skin model
    (skinmodel.py) for MelaninColor, so all the detail masks still apply."""
    for cat, hexcol in custom.items():
        if not hexcol:
            continue
        rgb = srgb_hex_to_linear(hexcol)
        if cat == "SkinColors":
            from . import skinmodel
            ov["scalar"]["MelaninAmount"] = 0.5
            params = dict(ov["vec3"])
            params.update(ov["scalar"])
            ov["vec3"]["MelaninColor"] = skinmodel.invert(rgb, params)[0]
        elif cat == "HairColors":
            for p in CUSTOM_PARAMS[cat]:
                ov["vec3"][p] = rgb
            ov["vec3"]["Hair_Scalp_Color"] = [c * 0.85 for c in rgb]
            ov["vec3"]["Beard_Scalp_Color"] = [c * 0.6 for c in rgb]
        elif cat == "ScleraColors":
            # the eye baker brightens authored sclera presets by 1.6
            ov["vec3"]["Eyes_ScleraColour"] = [min(1.0, c / 1.6) for c in rgb]
            ov["vec3"]["Eyes_ScleraColour_L"] = ov["vec3"]["Eyes_ScleraColour"]
            ov["scalar"]["Redness"] = min(ov["scalar"].get("Redness", 1.0), 0.6)
        elif cat == "EyeColors":
            ov["vec3"]["Eyes_IrisColour"] = rgb
            ov["vec3"]["Eyes_IrisSecondaryColour"] = [min(1.0, c * 1.1 + 0.03) for c in rgb]
        elif cat in COLOR_PARAMS:
            for p in COLOR_PARAMS[cat][0]:
                ov["vec3"][p] = rgb
