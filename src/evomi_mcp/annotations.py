"""Standard MCP tool annotations, as named presets.

`readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` are set
on every tool. `destructiveHint` and `openWorldHint` default to true when
omitted, so a tool that states none of them advertises itself as potentially
destructive and as reaching an open world of external entities.

`destructiveHint` and `idempotentHint` are documented as meaningful only when
`readOnlyHint` is false. They are still set on the read-only presets, because
"not meaningful" licenses a client to ignore them rather than guaranteeing it
will.
"""

from mcp.types import ToolAnnotations


def _hints(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> ToolAnnotations:
    """Build a ToolAnnotations with all four hints stated.

    Keyword-only with no defaults, so a preset cannot omit a hint and inherit
    the spec default.
    """
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


# A plain lookup against Evomi's own API: nothing changes, asking twice gives
# the same answer, and the set of things it can reach is fixed.
LOOKUP = _hints(read_only=True, destructive=False, idempotent=True, open_world=False)

# Read-only, but the answer is freshly minted rather than fetched: a generated
# session id or a server-side proxy list differs between two identical calls.
FRESH_LOOKUP = _hints(
    read_only=True, destructive=False, idempotent=False, open_world=False
)

# Fetches a URL the caller chose, so the reachable world is the whole web.
# Not idempotent: the page can change between calls, credits are spent each
# time, and a call carrying a storage config writes an object each time.
FETCH = _hints(read_only=True, destructive=False, idempotent=False, open_world=True)

# Creates a new stored entity. Additive — nothing existing is replaced — but
# calling it twice leaves two entities behind.
CREATE = _hints(read_only=False, destructive=False, idempotent=False, open_world=False)

# Replaces the contents of an existing stored entity. Destructive in the
# spec's sense (the previous contents are gone), and idempotent because
# applying the same update again lands on the same state.
UPDATE = _hints(read_only=False, destructive=True, idempotent=True, open_world=False)

# Removes a stored entity. Deleting an already-deleted entity leaves the same
# state, so it is idempotent even though the second call reports differently.
DELETE = _hints(read_only=False, destructive=True, idempotent=True, open_world=False)

# CREATE and UPDATE for the two tools whose work includes fetching a URL the
# caller named: a schema created or updated with `test` set is validated
# against the page it describes, and a config generated from a prompt is
# designed against the site it is for.
CREATE_OPEN_WORLD = _hints(
    read_only=False, destructive=False, idempotent=False, open_world=True
)
UPDATE_OPEN_WORLD = _hints(
    read_only=False, destructive=True, idempotent=True, open_world=True
)

# Flips a boolean. Nothing is destroyed, and it is emphatically not idempotent:
# a second call undoes the first.
TOGGLE = _hints(read_only=False, destructive=False, idempotent=False, open_world=False)

# Discards something live and unrecoverable — a sticky session's exit IP, which
# is released to the pool and cannot be asked for back, dropping whatever is
# currently connected — without creating or deleting a stored entity.
DISRUPTIVE = _hints(
    read_only=False, destructive=True, idempotent=False, open_world=False
)

# Creates something that costs money. `destructiveHint` is false because
# nothing that existed before is altered; no hint in the spec means "costs
# money", so the charge is carried by the description and the registration gate.
BILLABLE_CREATE = _hints(
    read_only=False, destructive=False, idempotent=False, open_world=False
)

# A natural-language pass-through to an Evomi-side agent that can do anything
# the rest of this surface can, including deleting saved configs and scraping
# arbitrary URLs, so its hints are the union of every tool it can stand in for.
OPEN_AGENT = _hints(
    read_only=False, destructive=True, idempotent=False, open_world=True
)
