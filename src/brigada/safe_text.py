"""Names that come from outside (real datasets) and end up in front of the model.

The lakehouse filters dish and restaurant names in Spark (``brigada_lakehouse.common.SAFE_NAME``);
this is the same rule for the Python side: the registry script that copies restaurant names into
Postgres and the run context that puts the name into the system prompt. A test checks both give
the same verdict on the same samples.

It shrinks what an external file can put in front of the model (no markup, no long payloads); it
does not make the text trusted: a short sentence of plain words still passes.
"""

from __future__ import annotations

import re

MAX_NAME_LENGTH = 80
# Letters of any script, digits, space and & ' ( ) + . / -, nothing else, 1 to 80 characters.
_SAFE_NAME = re.compile(rf"(?:[^\W_]|[ &'()+./-]){{1,{MAX_NAME_LENGTH}}}")


def is_safe_name(name: str | None) -> bool:
    return name is not None and _SAFE_NAME.fullmatch(name) is not None
