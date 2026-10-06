"""Names vulture reports as unused that are used in ways it cannot see.

Vulture parses this file and treats every name here as used. Add an entry only
with a reason; delete genuinely dead code instead of listing it.
"""

# sqlite3.Connection.row_factory is read by sqlite3 itself, not by our code.
_.row_factory
