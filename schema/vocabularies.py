"""Pinned vocabulary shared by contracts, SQL resources and drift checks."""

import json
from importlib.resources import files
from typing import Literal

VOCABULARY = json.loads(files("schema").joinpath("vocabularies/v1.json").read_text())
PERMISSIONS = tuple(VOCABULARY["permissions"])
MATERIAL_CLASSES = tuple(VOCABULARY["materials"])
MATERIALS = frozenset(MATERIAL_CLASSES)
MaterialClass = Literal[*MATERIAL_CLASSES]
