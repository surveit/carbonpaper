import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from pydantic import BaseModel

from docket.citations import Citation, parse_citation

_VERBS = (
    ("remove", re.compile(r"\bremov(?:e|ing)\b", re.IGNORECASE)),
    ("revise_republish", re.compile(r"\brevise and republish\b", re.IGNORECASE)),
    ("add", re.compile(r"\badd(?:ing)?\b", re.IGNORECASE)),
    ("revise", re.compile(r"\brevis(?:e|ing)\b", re.IGNORECASE)),
    ("amend", re.compile(r"\bamend(?:ed|ing)?\b", re.IGNORECASE)),
)


class ParsedProvision(BaseModel):
    citation: Citation
    heading: str
    body: str


class ParsedAmendment(BaseModel):
    citation: Citation
    verb: str
    instruction: str


def parse_regtext(
    xml_path: Path,
) -> tuple[list[ParsedProvision], list[ParsedAmendment]]:
    """Title and part come from the REGTEXT ancestor, so sections are never title-ambiguous."""
    root = ElementTree.parse(xml_path).getroot()
    provisions: list[ParsedProvision] = []
    amendments: list[ParsedAmendment] = []
    for block in _regtext_blocks(root):
        title, part = _block_scope(block)
        provisions += _provisions_in(block, title)
        amendments += _amendments_in(block, title)
    return provisions, amendments


def _regtext_blocks(root: ElementTree.Element) -> list[ElementTree.Element]:
    blocks = list(root.iter("REGTEXT"))
    return blocks if blocks else [root]


def _block_scope(block: ElementTree.Element) -> tuple[int, str]:
    return int(block.get("TITLE") or 40), block.get("PART") or ""


def _provisions_in(block: ElementTree.Element, title: int) -> list[ParsedProvision]:
    found: list[ParsedProvision] = []
    for section in block.iter("SECTION"):
        raw = _text_of(section.find("SECTNO"))
        citation = parse_citation(raw, title)
        if citation is None:
            continue
        found.append(
            ParsedProvision(
                citation=citation,
                heading=_text_of(section.find("SUBJECT")),
                body=_collapse(" ".join(section.itertext())),
            )
        )
    return found


_SECTION_TOKEN = re.compile(r"\b\d+\.\d+(?:-\d+)?[A-Za-z]*\b")


def _amendments_in(block: ElementTree.Element, title: int) -> list[ParsedAmendment]:
    found: list[ParsedAmendment] = []
    for node in block.iter("AMDPAR"):
        instruction = _collapse(" ".join(node.itertext()))
        verb = _read_verb(instruction)
        for raw in _targets_of(instruction):
            citation = parse_citation(raw, title)
            if citation is not None:
                found.append(
                    ParsedAmendment(
                        citation=citation, verb=verb, instruction=instruction[:400]
                    )
                )
    return found


def _targets_of(instruction: str) -> list[str]:
    """'Remove §§ 1037.140 and 1037.150' names two sections; only the first carries the § mark."""
    if "§" not in instruction:
        return []
    return _SECTION_TOKEN.findall(instruction)


def _read_verb(instruction: str) -> str:
    for name, pattern in _VERBS:
        if pattern.search(instruction):
            return name
    return "other"


def _text_of(node: ElementTree.Element | None) -> str:
    return _collapse(" ".join(node.itertext())) if node is not None else ""


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()
