"""Which rows a panel's figures are measured over. Shared by the tabs that offer both."""

from __future__ import annotations

from enum import Enum


class Basis(str, Enum):
    """`relevant` is the rows behind the cited figure; `all` is the whole frame."""

    relevant = "relevant"
    all = "all"
