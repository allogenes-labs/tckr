"""Identifier equivalence classes — curated alias groups, optionally persisted.

The recurring problem: one canonical entity, several identifiers. Concrete
instances across tckr consumers:

  - Polymarket renames a market's slug while the on-chain conditionId stays
    stable (`tckr.polymarket` keeps a dedicated slug -> conditionId map).
  - Hyperliquid spot is permissionless (HIP-1), so an unrelated token can
    squat a major's symbol; `tckr.hyperliquid.spot()` flags suspects via the
    spot/perp basis. A confirmed verdict ("this 'WLD' pair IS / IS NOT
    Worldcoin") is an equivalence fact worth persisting rather than
    re-deriving from basis every call.
  - CoinGecko ids vs exchange tickers ("worldcoin-wld" vs "WLD").
  - Dual-class equities (GOOG/GOOGL, BRK.A/BRK.B) for anyone using the
    options module.

`AliasMap` holds curated equivalence classes: every member of a group is an
acceptable identifier for any other member. Maps are intentionally small and
human-curated, NOT auto-derived — a false positive here silently conflates
two distinct assets, which is worse than a missed alias. Additions should
come from a human or a human-reviewed pipeline.

Persistence is opt-in via `TCKR_ALIASES_PATH` (one JSON file holding all
namespaces: `{"<namespace>": [["A", "B"], ...]}`). Manual edits to the file
survive restarts and win over code-supplied seeds. Unset (default) keeps
maps in-memory, matching the `tckr.polymarket` alias-map convention.

Design round-trip: this generalizes the `tckr.polymarket` alias pattern via
willard-trading's `newsfeed/aliases.py` (which borrowed it from tckr and
contributed back the transitive-merge fix in `add_group`).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from tckr import settings

log = logging.getLogger("tckr.aliases")


def _aliases_path() -> Path | None:
    """Resolve the configured aliases file path, or None for in-memory only."""
    raw = settings.ALIASES_PATH
    return Path(raw) if raw else None


def _load_file() -> dict[str, list[list[str]]]:
    """Read the whole aliases file. Empty dict when unset/missing/corrupt."""
    p = _aliases_path()
    if p is None or not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("aliases load failed (%s) — starting empty", e)
    return {}


class AliasMap:
    """Curated equivalence classes over string identifiers.

    `normalize` is applied to every identifier on the way in and on lookup.
    The default only strips whitespace — case-folding is deliberately NOT
    the default because identifier case is significant in several tckr
    namespaces (coingecko ids are lowercase, HL spot pair names like "@107"
    are case-sensitive). Equity consumers should pass `str.upper`.
    """

    def __init__(
        self,
        seed: Iterable[Iterable[str]] | None = None,
        normalize: Callable[[str], str] | None = None,
        namespace: str = "default",
    ):
        self._normalize = normalize or str.strip
        self._namespace = namespace
        # Disk wins over seed: manual curation in the file must not be
        # clobbered by whatever seed the importing code happens to ship.
        disk = _load_file().get(namespace)
        groups = disk if disk is not None else (seed or [])
        self._groups: list[set[str]] = [
            {self._normalize(s) for s in g} for g in groups
        ]
        self._groups = [g for g in self._groups if g]
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index: dict[str, int] = {
            t: i for i, g in enumerate(self._groups) for t in g
        }

    def aliases_of(self, ident: str) -> set[str]:
        """All identifiers equivalent to `ident`, including itself."""
        t = self._normalize(ident)
        i = self._index.get(t)
        if i is None:
            return {t}
        return set(self._groups[i])

    def are_aliased(self, a: str, b: str) -> bool:
        a, b = self._normalize(a), self._normalize(b)
        if a == b:
            return True
        i = self._index.get(a)
        return i is not None and b in self._groups[i]

    def add_group(self, idents: Iterable[str], persist: bool = True) -> None:
        """Add an equivalence class. Idempotent.

        A new group bridging several existing groups unions ALL of them —
        equivalence is transitive, so a partial merge would leave the index
        claiming two members of the same class are unrelated.
        """
        new = {self._normalize(s) for s in idents}
        new.discard("")
        if not new:
            return
        overlapping = [i for i, g in enumerate(self._groups) if g & new]
        for i in overlapping:
            new |= self._groups[i]
        self._groups = [
            g for i, g in enumerate(self._groups) if i not in overlapping
        ]
        self._groups.append(new)
        self._rebuild_index()
        if persist:
            self._persist()

    def groups(self) -> list[list[str]]:
        """Current classes as sorted lists (stable for display / diffing)."""
        return [sorted(g) for g in self._groups]

    def _persist(self) -> None:
        """Best-effort read-modify-write of our namespace in the shared file."""
        p = _aliases_path()
        if p is None:
            return
        try:
            data = _load_file()
            data[self._namespace] = self.groups()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:  # pragma: no cover — never block a caller on disk
            log.warning("aliases persist failed: %s", e)


# Namespace -> AliasMap, so independent consumers (HL spot, coingecko id
# mapping, equities) share one file without sharing equivalence classes.
_maps: dict[str, AliasMap] = {}


def get_map(
    namespace: str,
    seed: Iterable[Iterable[str]] | None = None,
    normalize: Callable[[str], str] | None = None,
) -> AliasMap:
    """Process-wide AliasMap for `namespace`, created on first use.

    `seed` and `normalize` only apply on first construction; later calls
    return the cached instance unchanged.
    """
    m = _maps.get(namespace)
    if m is None:
        m = AliasMap(seed=seed, normalize=normalize, namespace=namespace)
        _maps[namespace] = m
    return m
