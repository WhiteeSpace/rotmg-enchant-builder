#!/usr/bin/env python3
"""Detect new ROTMG enchantments from RealmEye without breaking the official DB.

This script does NOT rewrite existing enchantments. It parses the RealmEye
Enchanting page, verifies that every enchant currently present in data/enchants.json
is still found, then appends only truly new enchantments to data/enchants.json.

If RealmEye changes its HTML and the parser cannot recover the current database,
the script aborts safely and creates no PR-worthy DB change.

Usage:
  python scripts/detect_new_enchants.py
  python scripts/detect_new_enchants.py --source "RealmEye Enchanting Source.txt"
  python scripts/detect_new_enchants.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependency: beautifulsoup4. Install with: pip install beautifulsoup4", file=sys.stderr)
    raise

REALMEYE_URL = "https://www.realmeye.com/wiki/enchanting"
REALMEYE_BASE = "https://www.realmeye.com"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "enchants.json"
DEFAULT_REPORT_PATH = ROOT / "data" / "new_enchants_report.json"

ITEM_TYPES = ["WEAPON", "ABILITY", "ARMOR", "RING"]

GROUP_DEFINITIONS = [
    {"id": "mana", "name": "Mana / MP"},
    {"id": "life", "name": "Life / HP"},
    {"id": "attack", "name": "Attack / ATT"},
    {"id": "dexterity", "name": "Dexterity / Dex"},
    {"id": "defense", "name": "Defense / DEF"},
    {"id": "speed", "name": "Speed / SPD"},
    {"id": "vitality", "name": "Vitality / VIT"},
    {"id": "wisdom", "name": "Wisdom / WIS"},
    {"id": "weapon_damage", "name": "Weapon Damage"},
    {"id": "fire_rate", "name": "Fire rate"},
    {"id": "range", "name": "Projectile Range"},
    {"id": "ability", "name": "Ability / Cast"},
    {"id": "recovery", "name": "Recovery / Heal"},
    {"id": "proc", "name": "Procs"},
    {"id": "buffs", "name": "Temporary Buffs"},
    {"id": "reward", "name": "Loot / XP / Dust"},
    {"id": "tradeoff", "name": "Tradeoffs"},
    {"id": "awakened", "name": "Awakened"},
    {"id": "unique", "name": "Uniques / Special"},
    {"id": "armor_piercing", "name": "Armor Piercing"},
    {"id": "other", "name": "Other"},
]


def slugify(text: str) -> str:
    # Important: RealmEye names like "Shaitan’s Might" must become
    # "shaitan-s-might", matching the existing DB.
    text = text.lower().replace("’", "-").replace("'", "-")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "enchant"


def text_of(node: Any) -> str:
    """Extract compact text while preserving <br> boundaries as pipes."""
    raw = node.get_text("|", strip=True)
    raw = html_lib.unescape(raw)
    raw = re.sub(r"\|+", "|", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip(" |")


def normalize_effects(text: str) -> str:
    return text_of_text(text).replace("|", " | ")


def text_of_text(raw: str) -> str:
    raw = html_lib.unescape(raw)
    raw = re.sub(r"\|+", "|", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip(" |")


def fetch_realmeye_html() -> str:
    req = Request(
        REALMEYE_URL,
        headers={"User-Agent": "Mozilla/5.0 ROTMG-Enchant-Builder-Detector/2.0"},
    )
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def category_from_table(table: Any) -> str:
    th = table.find("th", attrs={"colspan": True})
    if th:
        bold = th.find("b")
        return text_of(bold if bold else th)

    previous_heading = table.find_previous(["h3", "h2"])
    if previous_heading:
        return text_of(previous_heading)

    return "Enchantments"


def tables_in_enchantments_section(soup: BeautifulSoup) -> list[Any]:
    start = soup.find(id="enchantments")
    if not start:
        return []

    tables: list[Any] = []
    current = start

    while True:
        current = current.find_next()
        if current is None:
            break

        # Stop before the page notes/history/trivia sections.
        if current.name == "h2" and current.get("id") in {"Notes", "history", "trivia"}:
            break

        if current.name == "table":
            tables.append(current)

    return tables


def split_labels(raw: str) -> list[str]:
    labels: list[str] = []
    for value in re.split(r"[|\s]+", raw):
        value = value.strip().upper()
        if value and value not in labels:
            labels.append(value)
    return labels


def split_eligible(raw: str) -> list[str]:
    values: list[str] = []
    for value in re.split(r"[|,\s]+", raw):
        value = value.strip().upper()
        if value in ITEM_TYPES or value == "ALL":
            if value not in values:
                values.append(value)
    return values or ["ALL"]


def first_icon_url(*nodes: Any) -> str:
    for node in nodes:
        image = node.find("img") if hasattr(node, "find") else None
        if image and image.get("src"):
            return urljoin(REALMEYE_BASE, html_lib.unescape(image["src"]))
    return ""


def infer_groups(name: str, category: str, effects: str, labels: list[str]) -> list[str]:
    joined = f"{name} {category} {effects} {' '.join(labels)}".lower()
    groups: list[str] = []

    checks = {
        "attack": [" att", "attack"],
        "defense": [" def", "defense"],
        "dexterity": [" dex", "dexterity"],
        "speed": [" spd", "speed"],
        "vitality": [" vit", "vitality"],
        "wisdom": [" wis", "wisdom"],
        "life": [" hp", "life", "max hp"],
        "mana": [" mp", "mana", "max mp"],
        "weapon_damage": ["weapon damage", "weapondamage", "damage"],
        "fire_rate": ["fire rate", "rate of fire", "weaponfirerate"],
        "range": ["range", "projectile speed", "projectile range", "weaponrange"],
        "ability": ["ability", "mp cost", "cooldown", "cost reduction", "casting"],
        "recovery": ["regen", "healing", "heal"],
        "proc": ["on shoot", "on hit", "proc", "fire ", "spawn ", "summon", "shooting"],
        "buffs": ["berserk", "damaging", "speedy", "inspired", "empowered"],
        "reward": ["loot", "xp", "dust", "reward"],
        "tradeoff": ["lowers", "minus", "tradeoff", "trade-off"],
        "awakened": ["awakened"],
        "unique": ["unique"],
        "armor_piercing": ["armor piercing"],
    }

    for group, needles in checks.items():
        if any(needle in joined for needle in needles):
            groups.append(group)

    return groups or ["other"]


def parse_realmeye_enchants(source_html: str) -> list[dict[str, Any]]:
    """Parse only the real enchantment tables.

    RealmEye has many unrelated tables on the same page. The only safe target is:
    h2#enchantments -> tables until Notes/History/Trivia.
    """
    soup = BeautifulSoup(source_html, "html.parser")
    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for table in tables_in_enchantments_section(soup):
        rows = table.find_all("tr")
        header_index: int | None = None
        headers: list[str] | None = None

        for index, row in enumerate(rows):
            cells = [text_of(cell).lower() for cell in row.find_all(["th", "td"])]
            joined = " ".join(cells)
            if "eligible items" in joined and "effect" in joined and "enchantment label" in joined:
                header_index = index
                headers = cells
                break

        if header_index is None or headers is None:
            continue

        category = category_from_table(table)

        # Two RealmEye formats exist:
        # 1) Basic tables: Name | Eligible | Effects | Labels | Incompatible
        # 2) Icon tables: Icon | Name | Eligible | Effects | Labels | Incompatible
        if len(headers) >= 6 and headers[0] == "enchantment" and headers[1] == "name":
            icon_col, name_col, eligible_col, effects_col, labels_col, incompatible_col = 0, 1, 2, 3, 4, 5
        else:
            icon_col, name_col, eligible_col, effects_col, labels_col, incompatible_col = 0, 0, 1, 2, 3, 4

        for row in rows[header_index + 1:]:
            cells = row.find_all(["td", "th"])
            required_col = max(name_col, eligible_col, effects_col, labels_col, incompatible_col)
            if len(cells) <= required_col:
                continue

            name = text_of(cells[name_col])
            if not name or name.lower() in {"name", "enchantment name"}:
                continue

            enchant_id = slugify(name)
            if enchant_id in seen_ids:
                continue

            eligible = split_eligible(text_of(cells[eligible_col]))
            effects = text_of(cells[effects_col]).replace("|", " | ")
            labels = split_labels(text_of(cells[labels_col]))
            incompatible = split_labels(text_of(cells[incompatible_col]))
            icon = first_icon_url(cells[icon_col], row)

            parsed.append(
                {
                    "id": enchant_id,
                    "name": name,
                    "category": category,
                    "eligible": eligible,
                    "effects": effects,
                    "labels": labels,
                    "incompatibleLabels": incompatible,
                    "icon": icon,
                    "groups": infer_groups(name, category, effects, labels),
                }
            )
            seen_ids.add(enchant_id)

    return parsed


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", help="Local RealmEye HTML source file. If omitted, downloads RealmEye.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to data/enchants.json.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="Path to detection report JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Only print/report; do not modify enchants.json.")
    args = parser.parse_args()

    db_path = Path(args.db)
    report_path = Path(args.report)

    if not db_path.exists():
        print(f"Missing database: {db_path}", file=sys.stderr)
        return 1

    current_db = load_json(db_path)
    current_enchants = current_db.get("enchants", [])

    if not isinstance(current_enchants, list):
        print("Invalid database: 'enchants' must be a list.", file=sys.stderr)
        return 1

    source_html = Path(args.source).read_text(encoding="utf-8", errors="replace") if args.source else fetch_realmeye_html()
    parsed = parse_realmeye_enchants(source_html)

    current_by_id = {entry.get("id"): entry for entry in current_enchants}
    parsed_by_id = {entry.get("id"): entry for entry in parsed}

    current_ids = set(current_by_id)
    parsed_ids = set(parsed_by_id)

    missing_current_ids = sorted(current_ids - parsed_ids)
    new_ids = sorted(parsed_ids - current_ids)

    status = "ok"
    message = "No new enchants detected."

    # Critical safety rule:
    # If the parser cannot find every current DB entry, it is not trusted.
    # It must not open a PR that could be based on a partial/broken parse.
    if missing_current_ids:
        status = "parser_incomplete"
        message = (
            f"Parser found {len(parsed)} RealmEye enchants, but missed "
            f"{len(missing_current_ids)} existing DB enchants. No DB changes made."
        )

        report = {
            "status": status,
            "message": message,
            "checkedAt": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "sourceUrl": REALMEYE_URL,
            "currentCount": len(current_enchants),
            "parsedRealmEyeCount": len(parsed),
            "missingCurrentCount": len(missing_current_ids),
            "missingCurrentIds": missing_current_ids,
            "newCount": 0,
            "newEnchantIds": [],
            "newEnchants": [],
        }
        write_json(report_path, report)
        print(message)
        return 0

    new_enchants = [parsed_by_id[enchant_id] for enchant_id in new_ids]

    report = {
        "status": status,
        "message": message if not new_enchants else f"Detected {len(new_enchants)} new enchant(s).",
        "checkedAt": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "sourceUrl": REALMEYE_URL,
        "currentCount": len(current_enchants),
        "parsedRealmEyeCount": len(parsed),
        "missingCurrentCount": 0,
        "missingCurrentIds": [],
        "newCount": len(new_enchants),
        "newEnchantIds": new_ids,
        "newEnchants": new_enchants,
    }
    write_json(report_path, report)

    if not new_enchants:
        print("No new enchants detected.")
        return 0

    if args.dry_run:
        print(f"Dry run: detected {len(new_enchants)} new enchant(s).")
        for enchant in new_enchants:
            print(f"- {enchant['name']} ({enchant['id']})")
        return 0

    # Preserve existing entries exactly as curated; only append new entries.
    current_db["enchants"] = current_enchants + new_enchants
    current_db["updatedFromOriginalHtml"] = dt.date.today().isoformat()
    current_db["sourceUrl"] = REALMEYE_URL
    current_db["lastRealmEyeDetection"] = {
        "checkedAt": report["checkedAt"],
        "parsedRealmEyeCount": len(parsed),
        "newCount": len(new_enchants),
        "newEnchantIds": new_ids,
    }

    if "groupDefinitions" not in current_db:
        current_db["groupDefinitions"] = GROUP_DEFINITIONS

    write_json(db_path, current_db)

    print(f"Detected {len(new_enchants)} new enchant(s) and updated {db_path}.")
    for enchant in new_enchants:
        print(f"- {enchant['name']} ({enchant['id']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
