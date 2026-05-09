"""
Rules Store
Persistent JSON-based storage for extracted rules and conflicts.
In production this would be a proper DB, but JSON keeps the demo dependency-free.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Iterator

from app.models.schemas import ExtractedRule, Conflict
from app.utils.config import settings

logger = logging.getLogger(__name__)


class RulesStore:
    """Simple persistent store for extracted rules and conflicts."""

    def __init__(self, path: str | None = None):
        self._path = Path(path or settings.RULES_STORE_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                    # Migrate old format
                    if "rules" not in raw:
                        raw = {"rules": [], "conflicts": [], "meta": {}}
                    return raw
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Rules store corrupted, reinitializing: {e}")
        return {"rules": [], "conflicts": [], "meta": {}}

    def _save(self) -> None:
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, default=str)

    # ---- Rules ----

    def save_rules(self, rules: list[ExtractedRule], policy_name: str = "default") -> None:
        """Persist extracted rules (overwrites existing for same policy)."""
        serialized = [json.loads(r.model_dump_json()) for r in rules]
        # Remove old rules for this policy
        self._data["rules"] = [
            r for r in self._data["rules"]
            if r.get("_policy") != policy_name
        ]
        for r in serialized:
            r["_policy"] = policy_name
        self._data["rules"].extend(serialized)
        self._data["meta"][policy_name] = {
            "extracted_at": datetime.utcnow().isoformat(),
            "rule_count": len(rules),
        }
        self._save()
        logger.info(f"Saved {len(rules)} rules for policy '{policy_name}'")

    def load_rules(self, policy_name: str | None = None) -> list[ExtractedRule]:
        """Load rules, optionally filtered by policy name."""
        raw_rules = self._data.get("rules", [])
        if policy_name:
            raw_rules = [r for r in raw_rules if r.get("_policy") == policy_name]
        
        rules: list[ExtractedRule] = []
        for raw in raw_rules:
            try:
                # Remove internal fields before parsing
                clean = {k: v for k, v in raw.items() if not k.startswith("_")}
                rules.append(ExtractedRule.model_validate(clean))
            except Exception as e:
                logger.warning(f"Could not deserialize rule: {e}")
        return rules

    def get_rule(self, rule_id: str) -> ExtractedRule | None:
        """Get a single rule by ID."""
        for raw in self._data.get("rules", []):
            if raw.get("rule_id") == rule_id:
                try:
                    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
                    return ExtractedRule.model_validate(clean)
                except Exception:
                    return None
        return None

    def iter_rules_by_category(self, category: str) -> Iterator[ExtractedRule]:
        """Iterate rules for a specific category."""
        for rule in self.load_rules():
            if rule.category == category:
                yield rule

    # ---- Conflicts ----

    def save_conflicts(self, conflicts: list[Conflict], policy_name: str = "default") -> None:
        serialized = [json.loads(c.model_dump_json()) for c in conflicts]
        self._data["conflicts"] = [
            c for c in self._data.get("conflicts", [])
            if c.get("_policy") != policy_name
        ]
        for c in serialized:
            c["_policy"] = policy_name
        self._data["conflicts"].extend(serialized)
        self._save()

    def load_conflicts(self, policy_name: str | None = None) -> list[Conflict]:
        raw_conflicts = self._data.get("conflicts", [])
        if policy_name:
            raw_conflicts = [c for c in raw_conflicts if c.get("_policy") == policy_name]
        conflicts: list[Conflict] = []
        for raw in raw_conflicts:
            try:
                clean = {k: v for k, v in raw.items() if not k.startswith("_")}
                conflicts.append(Conflict.model_validate(clean))
            except Exception as e:
                logger.warning(f"Could not deserialize conflict: {e}")
        return conflicts

    # ---- Meta ----

    def list_policies(self) -> list[str]:
        return list(self._data.get("meta", {}).keys())

    def get_policy_meta(self, policy_name: str) -> dict:
        return self._data.get("meta", {}).get(policy_name, {})

    def clear(self, policy_name: str | None = None) -> None:
        if policy_name:
            self._data["rules"] = [
                r for r in self._data["rules"] if r.get("_policy") != policy_name
            ]
            self._data["conflicts"] = [
                c for c in self._data.get("conflicts", []) if c.get("_policy") != policy_name
            ]
            self._data["meta"].pop(policy_name, None)
        else:
            self._data = {"rules": [], "conflicts": [], "meta": {}}
        self._save()


# Singleton instance
_store: RulesStore | None = None


def get_store() -> RulesStore:
    global _store
    if _store is None:
        _store = RulesStore()
    return _store