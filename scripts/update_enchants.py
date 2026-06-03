#!/usr/bin/env python3
"""Update the ROTMG enchant database from RealmEye.

This script is designed for GitHub Actions or a local cron job. It downloads the
RealmEye enchanting page, tries to rebuild data/enchants.json in the same schema
used by the builder, and keeps the previous database as a safe fallback if the
page layout changes.

Usage:
  python scripts/update_enchants.py
  python scripts/update_enchants.py --source data/realmeye_enchanting.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REALMEYE_URL = "https://www.realmeye.com/wiki/enchanting"
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "enchants.json"
RAW_PATH = DATA_DIR / "realmeye_enchanting.html"
META_PATH = DATA_DIR / "enchants.meta.json"

ITEM_TYPES = ["WEAPON", "ABILITY", "ARMOR", "RING"]
STAT_GROUPS = {
    "ATT": "attack", "Attack": "attack",
    "DEF": "defense", "Defense": "defense",
    "DEX": "dexterity", "Dexterity": "dexterity",
    "SPD": "speed", "Speed": "speed",
    "VIT": "vitality", "Vitality": "vitality",
    "WIS": "wisdom", "Wisdom": "wisdom",
    "HP": "life", "Life": "life", "Max HP": "life",
    "MP": "mana", "Mana": "mana", "Max MP": "mana",
}
GROUP_DEFINITIONS = [
    {"id":"mana","name":"Mana / MP"}, {"id":"life","name":"Life / HP"},
    {"id":"attack","name":"Attack / ATT"}, {"id":"dexterity","name":"Dexterity / Dex"},
    {"id":"defense","name":"Defense / DEF"}, {"id":"speed","name":"Speed / SPD"},
    {"id":"vitality","name":"Vitality / VIT"}, {"id":"wisdom","name":"Wisdom / WIS"},
    {"id":"weapon_damage","name":"Weapon Damage"}, {"id":"fire_rate","name":"Fire rate"},
    {"id":"range","name":"Projectile Range"}, {"id":"ability","name":"Ability / Cast"},
    {"id":"recovery","name":"Recovery / Heal"}, {"id":"proc","name":"Procs"},
    {"id":"buffs","name":"Temporary Buffs"}, {"id":"reward","name":"Loot / XP / Dust"},
    {"id":"tradeoff","name":"Tradeoffs"}, {"id":"awakened","name":"Awakened"},
    {"id":"unique","name":"Uniques / Special"}, {"id":"other","name":"Other"},
]


def slugify(text: str) -> str:
    text = re.sub(r"[’']", "", text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "enchant"


def clean_html_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", " | ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def fetch_html() -> str:
    req = Request(REALMEYE_URL, headers={"User-Agent":"Mozilla/5.0 ROTMG-Enchant-Builder-Updater/1.0"})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def infer_groups(text: str, labels: list[str]) -> list[str]:
    joined = " ".join([text, *labels])
    groups: list[str] = []
    for key, group in STAT_GROUPS.items():
        if re.search(rf"\b{re.escape(key)}\b", joined, flags=re.I) and group not in groups:
            groups.append(group)
    lower = joined.lower()
    checks = [
        ("weapon_damage", ["weapon damage", "damage", "weapondamage"]),
        ("fire_rate", ["fire rate", "rate of fire", "firerate", "weaponfirerate"]),
        ("range", ["range", "projectile", "lifetime", "speedy shots", "weaponrange"]),
        ("ability", ["ability", "mp cost", "cost reduction", "cooldown"]),
        ("reward", ["loot", "xp", "dust", "reward"]),
        ("proc", ["on shoot", "on hit", "proc", "bleeding", "shot", "shooting"]),
        ("awakened", ["awakened"]),
        ("unique", ["unique"]),
        ("tradeoff", ["tradeoff", "trade-off", "lowers", "minus"]),
    ]
    for group, words in checks:
        if any(w in lower for w in words) and group not in groups:
            groups.append(group)
    return groups or ["other"]


def parse_tables(html: str) -> list[dict[str, Any]]:
    """Best-effort RealmEye parser.

    RealmEye's wiki markup changes occasionally. This parser intentionally uses
    conservative table heuristics. If too few enchants are found, the script
    aborts and preserves the last good JSON.
    """
    enchants: list[dict[str, Any]] = []
    # Split into sections so headings become categories.
    parts = re.split(r"(<h[2-4][^>]*>.*?</h[2-4]>)", html, flags=re.I | re.S)
    category = "Enchantment"
    sections = []
    it = iter(parts)
    pre = next(it, "")
    for heading, body in zip(it, it):
        htext = clean_html_text(heading)
        if htext:
            category = htext
        sections.append((category, body))
    if not sections:
        sections = [("Enchantment", html)]

    seen: set[str] = set()
    for category, body in sections:
        for table in re.findall(r"<table[^>]*>(.*?)</table>", body, flags=re.I | re.S):
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.I | re.S)
            if not rows:
                continue
            headers = [clean_html_text(c).lower() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], flags=re.I | re.S)]
            for row in rows[1:]:
                cells_raw = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.I | re.S)
                cells = [clean_html_text(c) for c in cells_raw]
                if len(cells) < 2:
                    continue
                name = cells[0]
                if not name or len(name) > 80 or name.lower() in {"name", "enchantment"}:
                    continue
                joined = " ".join(cells)
                # Avoid parsing item rows that do not look like enchantments.
                if not any(tok in joined.lower() for tok in ["att", "def", "dex", "spd", "vit", "wis", "hp", "mp", "%", "damage", "loot", "dust", "range", "unique", "awakened", "label"]):
                    continue
                sid = slugify(name)
                if sid in seen:
                    continue
                seen.add(sid)
                eligible = [t for t in ITEM_TYPES if re.search(rf"\b{t}\b", joined, flags=re.I)] or ["ALL"]
                labels = []
                for label in re.findall(r"\b[A-Z][A-Z0-9]{2,}\b", joined):
                    if label not in ITEM_TYPES and label not in labels:
                        labels.append(label)
                if "Unique" in joined and "UNIQUE" not in labels:
                    labels.append("UNIQUE")
                if "Awakened" in joined and "AWAKENED" not in labels:
                    labels.append("AWAKENED")
                effects = cells[1] if len(cells) == 2 else " ".join(cells[1:])
                enchants.append({
                    "id": sid,
                    "name": name,
                    "category": category,
                    "eligible": eligible,
                    "effects": effects,
                    "labels": labels,
                    "incompatibleLabels": labels[:1] if labels else [],
                    "icon": "",
                    "groups": infer_groups(joined, labels),
                })
    return enchants


def merge_with_previous(new_db: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return new_db
    prev_by_id = {e.get("id"): e for e in previous.get("enchants", [])}
    for e in new_db["enchants"]:
        old = prev_by_id.get(e["id"])
        if not old:
            continue
        # Preserve hand-curated fields that RealmEye table parsing may not expose.
        for key in ("icon", "groups", "incompatibleLabels", "eligible", "awakenedItems"):
            if old.get(key) and (not e.get(key) or key in {"icon", "awakenedItems"}):
                e[key] = old[key]
    return new_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="Use a local RealmEye HTML file instead of downloading.")
    ap.add_argument("--min-count", type=int, default=120, help="Abort if fewer enchants are parsed.")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    previous = json.loads(DB_PATH.read_text(encoding="utf-8")) if DB_PATH.exists() else None
    html = Path(args.source).read_text(encoding="utf-8") if args.source else fetch_html()
    RAW_PATH.write_text(html, encoding="utf-8")
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    enchants = parse_tables(html)
    if len(enchants) < args.min_count:
        print(f"Parsed only {len(enchants)} enchants; keeping previous database.", file=sys.stderr)
        META_PATH.write_text(json.dumps({
            "status":"parse_failed",
            "source":REALMEYE_URL,
            "htmlSha256":html_hash,
            "parsedCount":len(enchants),
            "checkedAt":dt.datetime.utcnow().isoformat(timespec="seconds")+"Z",
        }, indent=2), encoding="utf-8")
        return 2
    today = dt.date.today().isoformat()
    db = {
        "source": "RealmEye enchanting HTML",
        "sourceUrl": REALMEYE_URL,
        "generatedFrom": "scripts/update_enchants.py",
        "updatedFromOriginalHtml": today,
        "generatedAt": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "htmlSha256": html_hash,
        "itemTypes": ITEM_TYPES,
        "enchants": enchants,
        "groupDefinitions": previous.get("groupDefinitions", GROUP_DEFINITIONS) if previous else GROUP_DEFINITIONS,
    }
    db = merge_with_previous(db, previous)
    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    META_PATH.write_text(json.dumps({"status":"ok", "source":REALMEYE_URL, "htmlSha256":html_hash, "parsedCount":len(db["enchants"]), "checkedAt":db["generatedAt"]}, indent=2), encoding="utf-8")
    print(f"Updated {DB_PATH} with {len(db['enchants'])} enchants.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
