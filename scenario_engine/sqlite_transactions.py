"""Short SQLite writes that preserve ownership of an enclosing transaction."""
from contextlib import contextmanager


@contextmanager
def atomic_write(connection):
    nested = connection.in_transaction
    if nested:
        connection.execute("SAVEPOINT stockm1_write")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            connection.execute("RELEASE stockm1_write")
        else:
            connection.commit()
    except BaseException:
        if nested:
            connection.execute("ROLLBACK TO stockm1_write")
            connection.execute("RELEASE stockm1_write")
        else:
            connection.rollback()
        raise


def create_tables(connection, schema):
    """Execute static DDL without executescript's implicit commit."""
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


@contextmanager
def read_snapshot(connection):
    """Use one committed view across evidence selection and reconciliation queries."""
    connection.execute("SAVEPOINT stockm1_read")
    try:
        yield
    finally:
        connection.execute("RELEASE stockm1_read")
