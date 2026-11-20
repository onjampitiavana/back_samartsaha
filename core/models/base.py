

from typing import Any, Dict, List, Optional

Row = Dict[str, Any]


class BaseRepository:
    """Shared helpers for the table repositories."""

    TABLE: str = ""

    def __init__(self, client: Any) -> None:
        self.client = client

    def query(self):
        """A fresh PostgREST request builder on this repository's table."""
        return self.client.table(self.TABLE)

    @staticmethod
    def rows(response: Any) -> List[Row]:
        """The rows of a PostgREST response ([] when empty)."""
        return list(getattr(response, "data", None) or [])

    @classmethod
    def first(cls, response: Any) -> Optional[Row]:
        """The first row of a PostgREST response, or None."""
        data = cls.rows(response)
        return data[0] if data else None

    def count(self) -> int:
        """Number of rows visible to the client, counted by the database
        (no row is transferred)."""
        response = self.query().select("uuid", count="exact", head=True).execute()
        return int(getattr(response, "count", None) or 0)

    def count_where(self, column: str, value: Any) -> int:
        """Rows visible to the client whose `column` equals `value`, counted
        by the database (no row is transferred)."""
        response = (self.query().select(column, count="exact", head=True)
                    .eq(column, value).execute())
        return int(getattr(response, "count", None) or 0)

    def delete_by(self, column: str, value: Any) -> List[Row]:
        """Delete the rows whose `column` equals `value`; returns the deleted
        rows ([] if none existed or the client may not delete them)."""
        return self.rows(self.query().delete().eq(column, value).execute())

    def insert(self, row: Row) -> Optional[Row]:
        """Insert one row; returns it as stored (or None if not returned)."""
        return self.first(self.query().insert(row).execute())
