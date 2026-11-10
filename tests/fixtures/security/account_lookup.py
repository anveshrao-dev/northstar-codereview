"""Fictional Northstar DB-API examples; source data, never executed in tests."""


def find_account(cursor, request_name):
    """Look up a name received directly from an untrusted request parameter."""
    query = "SELECT account_id FROM accounts WHERE name = '" + request_name + "'"
    return cursor.execute(query).fetchall()


def find_account_parameterized(cursor, request_name):
    """This cursor's DB-API driver uses question-mark value placeholders."""
    return cursor.execute(
        "SELECT account_id FROM accounts WHERE name = ?", (request_name,)
    ).fetchall()


def list_audit_rows(cursor, table_name):
    """Internal reporting helper; caller and table selection policy are elsewhere."""
    query = "SELECT event_id FROM " + table_name
    return cursor.execute(query).fetchall()
