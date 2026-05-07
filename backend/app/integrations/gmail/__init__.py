"""Gmail integration: search, read, and send messages on the user's behalf.

The integration uses Gmail's REST API with two OAuth scopes:

* ``gmail.readonly`` for ``messages.list`` and ``messages.get``
* ``gmail.send`` for composing new mail and threaded replies

The factory registers four agent tools (``gmail_search``, ``gmail_get_message``,
``gmail_list_recent``, ``gmail_send``) all defaulting to ``ask`` permission so
the user is prompted before any mailbox access or outbound message.
"""

from backend.app.integrations.gmail import factory  # noqa: F401  (registers tools)
