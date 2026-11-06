"""trino-tags: every query of a call carries its request id and its tool as client tags.

Trino keeps the client tags of a query (``X-Trino-Client-Tags``) with it: its query history,
its resource groups (a selector on ``clientTags``) and a policy engine that records them see
which call of the server sent each query. The tags are ``request_id=<id>``, the id of the
request the edge sent or the server made, and ``tool=<tool>``, each only when known. The core
keeps an edge's request id only when it is a plain token (letters, digits, ``.``, ``_``,
``:``, ``-``), and a tool name only when it is one too, else ``tool=other``: no comma and no
``=`` can add a tag of its own. The filter's tag
(``mcp-gate``) and the profile's (``mcp-profile``) come in addition.
"""

import re
from typing import Mapping

from ..audit import current_call, current_request_id
from ..config import Config
from ..extensions import Extension

# A tool name a tag may carry as it is: anything else (a plugin's tool named with a comma, an
# `=` or a space) could add a tag of its own, and is said as `tool=other`.
_TOOL_NAME = re.compile(r"[A-Za-z0-9._:-]+")


class TrinoTagsExtension(Extension):
    """The request id and the tool of the call, on each query it sends."""

    name = "trino-tags"

    def check_lines(self) -> list[str]:
        """The tags each query carries."""
        return ["trino-tags: every query carries request_id=<id> and tool=<tool> as client tags"]

    def client_tags(self) -> list[str]:
        """The client tags of a query run now: the request id and the tool, when known."""
        tags = []
        request_id = current_request_id()
        if request_id:
            tags.append(f"request_id={request_id}")
        call = current_call()
        if call is not None:
            tool = call.tool if _TOOL_NAME.fullmatch(call.tool) else "other"
            tags.append(f"tool={tool}")
        return tags


def create(config: Config, env: Mapping[str, str]) -> TrinoTagsExtension:
    """The module; it reads no setting of its own."""
    return TrinoTagsExtension()
