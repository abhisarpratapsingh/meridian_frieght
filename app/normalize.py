"""Canonicalization helpers — the core of entity resolution.

Vehicle registrations arrive in the wild: "UP-40-IM-3144", "up86cm7252",
"HR 13 HG 1333", "CH40IK6238". canonical_reg() collapses all of these to one
uppercase, punctuation-free key so every source can be joined on it.
"""
import re

_STRIP = re.compile(r"[\s\-]")


def canonical_reg(raw: str) -> str:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _STRIP.sub("", s).upper()
    return s or None


CLIENT_ALIASES = {
    "shakti cement": "Shakti Cement",
    "shakti": "Shakti Cement",
    "vertex retail": "Vertex Retail",
    "vertex": "Vertex Retail",
    "apex chemicals": "Apex Chemicals",
    "apex": "Apex Chemicals",
    "orion pharma": "Orion Pharma",
    "orion": "Orion Pharma",
    "internal": "Internal",
}


def canonical_client(raw: str) -> str:
    if raw is None:
        return None
    key = str(raw).strip().lower()
    return CLIENT_ALIASES.get(key, str(raw).strip())


HUB_ALIASES = {
    "delhi": "Delhi", "gurgaon": "Gurgaon", "faridabad": "Faridabad", "noida": "Noida",
    "ambala": "Ambala", "chandigarh": "Chandigarh", "rudrapur": "Rudrapur",
    "kanpur": "Kanpur", "lucknow": "Lucknow", "jaipur": "Jaipur", "ludhiana": "Ludhiana",
}


def canonical_hub(raw: str) -> str:
    if raw is None or str(raw).strip() == "":
        return None
    key = str(raw).strip().lower()
    return HUB_ALIASES.get(key, str(raw).strip())


# Delhi-NCR routes are the ones covered by the winter BS4 ban (dispatcher transcript, line 14).
NCR_HUBS = {"Delhi", "Gurgaon", "Faridabad", "Noida"}

# Hill route hubs (transcript line 18: Rudrapur and "anything going up toward Nainital side").
HILL_HUBS = {"Rudrapur"}

# ASSUMPTION-02 (see EVIDENCE.md): no geo-coordinates are provided anywhere in the
# bundle, so "nearest hub" beyond the origin hub cannot be computed from real
# distances. We use a documented, human-authored adjacency table based on actual
# North-India geography (Delhi-NCR cluster, Punjab/Haryana/Chandigarh cluster,
# UP cluster, Rajasthan) as the search order after the origin hub itself.
HUB_ADJACENCY = {
    "Delhi": ["Gurgaon", "Faridabad", "Noida", "Ambala"],
    "Gurgaon": ["Delhi", "Faridabad", "Ambala"],
    "Faridabad": ["Delhi", "Gurgaon", "Noida"],
    "Noida": ["Delhi", "Faridabad"],
    "Ambala": ["Chandigarh", "Delhi", "Gurgaon"],
    "Chandigarh": ["Ambala", "Rudrapur"],
    "Rudrapur": ["Chandigarh", "Kanpur", "Lucknow"],
    "Kanpur": ["Lucknow", "Rudrapur"],
    "Lucknow": ["Kanpur", "Rudrapur"],
    "Jaipur": ["Delhi", "Ludhiana"],
    "Ludhiana": ["Chandigarh", "Ambala", "Jaipur"],
}


def hub_search_order(origin_hub: str) -> list:
    """Origin hub first, then documented adjacency, then every remaining hub
    (alphabetical, for determinism) as last resort."""
    all_hubs = sorted(HUB_ADJACENCY.keys())
    order = [origin_hub] if origin_hub else []
    order += [h for h in HUB_ADJACENCY.get(origin_hub, []) if h not in order]
    order += [h for h in all_hubs if h not in order]
    return order
