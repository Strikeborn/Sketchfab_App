from __future__ import annotations
import re
import logging
import typing as t
from dataclasses import dataclass

import yaml
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

@dataclass
class MatchSignals:
    tag_hits: set[str]
    rule_hits: set[str]
    fuzzy_hits: dict[str, int]  # collection -> score

@dataclass
class PolicyResult:
    assigned: list[str]
    notes: str


class Terms:
    def __init__(self, cfg: dict):
        self.single = set(cfg.get("single_assignment_collections", []) or [])
        self.negative = set(map(str.lower, cfg.get("negative_terms", []) or []))
        self.collections: dict[str, dict] = cfg.get("collections", {})

    @classmethod
    def from_yaml(cls, path: str) -> "Terms":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data)


def normalize_text(*parts: str | None) -> str:
    text = " ".join([p or "" for p in parts])
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def collect_signals(model_name: str, description: str | None, tags: list[str], terms: Terms) -> MatchSignals:
    name_desc = normalize_text(model_name, description, ",".join(tags))
    tokens = set(name_desc.split())
    tagset = set([t.lower() for t in tags])

    tag_hits: set[str] = set()
    rule_hits: set[str] = set()
    fuzzy_hits: dict[str, int] = {}

    for coll_name, cfg in terms.collections.items():
        include_terms = set([s.lower() for s in (cfg.get("include_terms") or [])])
        tag_terms = set([s.lower() for s in (cfg.get("tag_terms") or [])])
        exclude_terms = set([s.lower() for s in (cfg.get("exclude_terms") or [])])
        threshold = int(cfg.get("fuzzy_threshold", 88))

        # Negative guards
        if any(nt in name_desc for nt in terms.negative):
            continue
        if any(et in name_desc for et in exclude_terms):
            continue

        # Tag hits (exact tag terms)
        if tag_terms & tagset:
            tag_hits.add(coll_name)

        # Rule hits (token presence)
        if include_terms & tokens:
            rule_hits.add(coll_name)

        # Fuzzy matches across include terms (try to catch near matches in text)
        best_score = 0
        for it in include_terms:
            score = fuzz.partial_ratio(it, name_desc)
            if score > best_score:
                best_score = score
        if best_score >= threshold:
            fuzzy_hits[coll_name] = best_score

    return MatchSignals(tag_hits=tag_hits, rule_hits=rule_hits, fuzzy_hits=fuzzy_hits)


def policy_assign(signals: MatchSignals, terms: Terms) -> PolicyResult:
    # Consensus-based policy
    votes: dict[str, int] = {}
    for c in signals.tag_hits:
        votes[c] = votes.get(c, 0) + 1
    for c in signals.rule_hits:
        votes[c] = votes.get(c, 0) + 1
    for c, _ in signals.fuzzy_hits.items():
        votes[c] = votes.get(c, 0) + 1

    strong_fuzzy = {c for c, s in signals.fuzzy_hits.items() if s >= 95}

    assigned = set()
    notes = []

    # Primary rule: assign if votes >= 2 or strong fuzzy
    for c, v in votes.items():
        if v >= 2 or c in strong_fuzzy:
            assigned.add(c)

    # Enforce single-assignment collisions
    single_hits = [c for c in assigned if c in terms.single]
    if len(single_hits) > 1:
        # keep none—force human review
        notes.append(f"Single-assignment collision: {', '.join(single_hits)}")
        for c in single_hits:
            assigned.discard(c)

    # If nothing assigned but we have a solid vote of 1 + high fuzzy, leave unassigned to be safe
    if not assigned and signals.fuzzy_hits:
        top = max(signals.fuzzy_hits.items(), key=lambda kv: kv[1])
        notes.append(f"High fuzzy candidate: {top[0]} ({top[1]})")

    return PolicyResult(assigned=sorted(assigned), notes="; ".join(notes))