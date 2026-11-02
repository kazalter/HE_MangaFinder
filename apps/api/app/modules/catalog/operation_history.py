"""Record reversible manual catalog edits, including SQLite FK cascades.

The temporary triggers live only for one SQLite connection and transaction.
History is deliberately private: raw rows may contain source metadata.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CatalogOperation

TRACKED = (
    "work_groups",
    "work_group_members",
    "works",
    "work_sources",
    "work_fingerprints",
    "author_works",
    "author_work_exclusions",
    "merge_suggestions",
    "agent_reviews",
    "catalog_events",
    "release_signals",
    "planned_releases",
    "work_relations",
    "series_members",
)
LOG_TABLE = "_mangafinder_operation_log"


def lock_catalog_write(session: Session) -> None:
    """Serialize SQLite catalog decisions before reading affected rows."""
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


class UndoConflict(ValueError):
    """The affected data changed after the operation and cannot be safely restored."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(connection, table: str) -> list[tuple[str, int]]:
    return [
        (row[1], row[5])
        for row in connection.exec_driver_sql(f"PRAGMA table_info({_quote(table)})")
    ]


def _json_row(columns: list[tuple[str, int]], prefix: str) -> str:
    return (
        "json_object("
        + ",".join(
            f"'{name.replace(chr(39), chr(39) * 2)}',{prefix}{_quote(name)}" for name, _ in columns
        )
        + ")"
    )


def _reference_state(connection, parent_ids: dict[str, set[int]]) -> dict:
    """Capture identities of every row referencing affected works and groups."""
    result = {}
    tables = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (child,) in tables:
        foreign_keys = connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote(child)})"
        ).fetchall()
        grouped = {}
        for fk in foreign_keys:
            if fk[2] in parent_ids:
                grouped.setdefault(fk[0], []).append(fk)
        if not grouped:
            continue
        child_columns = _columns(connection, child)
        child_pk = [
            name for name, order in sorted(child_columns, key=lambda item: item[1]) if order
        ]
        if not child_pk:
            raise UndoConflict(f"无法检查 {child} 的关联：缺少主键")
        selection = ",".join(_quote(name) for name in child_pk)
        for fk_id, items in grouped.items():
            if len(items) != 1 or items[0][4] not in (None, "id"):
                raise UndoConflict("数据库关联结构不受支持")
            fk = items[0]
            for parent_id in sorted(parent_ids[fk[2]]):
                rows = connection.exec_driver_sql(
                    f"SELECT {selection} FROM {_quote(child)} WHERE {_quote(fk[3])}=?",
                    (parent_id,),
                ).fetchall()
                key = f"{fk[2]}:{parent_id}:{child}:{fk_id}"
                result[key] = sorted([list(row) for row in rows], key=lambda row: json.dumps(row))
    return result


def _affected_parents(changes: list[dict], group_ids: list[int]) -> dict[str, set[int]]:
    parents = {"work_groups": set(group_ids), "works": set()}
    for change in changes:
        for row in (change["before"], change["after"]):
            if row is None:
                continue
            if change["table"] in parents and isinstance(row.get("id"), int):
                parents[change["table"]].add(row["id"])
            if change["table"] == "work_group_members" and isinstance(row.get("work_id"), int):
                parents["works"].add(row["work_id"])
    return parents


class OperationJournal:
    def __init__(self, session: Session, kind: str, group_ids: list[int], summary: str):
        self.session = session
        self.connection = session.connection()
        self.operation = CatalogOperation(
            kind=kind,
            group_ids=group_ids,
            before_group_ids=group_ids,
            after_group_ids=[],
            summary=summary,
            changes=[],
        )
        session.add(self.operation)
        session.flush()  # Acquire the SQLite write lock before capturing changes.
        self.connection.exec_driver_sql(
            f"CREATE TEMP TABLE {_quote(LOG_TABLE)} (seq INTEGER PRIMARY KEY, "
            "table_name TEXT NOT NULL, action TEXT NOT NULL, before_row TEXT, after_row TEXT)"
        )
        for table in TRACKED:
            columns = _columns(self.connection, table)
            if not columns:
                raise ValueError(f"无法记录操作：缺少 {table} 表")
            for action in ("INSERT", "UPDATE", "DELETE"):
                old = _json_row(columns, "OLD.") if action != "INSERT" else "NULL"
                new = _json_row(columns, "NEW.") if action != "DELETE" else "NULL"
                trigger = _quote(f"_mf_{table}_{action.lower()}")
                self.connection.exec_driver_sql(
                    f"CREATE TEMP TRIGGER {trigger} AFTER {action} ON main.{_quote(table)} "
                    f"BEGIN INSERT INTO {_quote(LOG_TABLE)} "
                    "(table_name, action, before_row, after_row) "
                    f"VALUES ('{table}', '{action}', {old}, {new}); END"
                )

    def finish(self, result_group_ids: list[int] | None = None) -> CatalogOperation:
        self.session.flush()
        rows = self.connection.exec_driver_sql(
            "SELECT table_name, action, before_row, after_row "
            f"FROM {_quote(LOG_TABLE)} ORDER BY seq"
        ).fetchall()
        changes = [
            {
                "table": table,
                "action": action,
                "before": json.loads(before) if before else None,
                "after": json.loads(after) if after else None,
            }
            for table, action, before, after in rows
        ]
        if len(changes) > 10000 or len(json.dumps(changes).encode()) > 20 * 1024 * 1024:
            raise ValueError("操作涉及的数据过多，无法安全记录撤销信息")
        self.operation.changes = changes
        if result_group_ids:
            self.operation.group_ids = sorted(set(self.operation.group_ids + result_group_ids))
        parents = _affected_parents(changes, self.operation.group_ids)
        self.operation.reference_state = {
            "parents": {table: sorted(ids) for table, ids in parents.items()},
            "references": _reference_state(self.connection, parents),
        }
        for table in TRACKED:
            for action in ("insert", "update", "delete"):
                self.connection.exec_driver_sql(f"DROP TRIGGER {_quote(f'_mf_{table}_{action}')}")
        self.connection.exec_driver_sql(f"DROP TABLE {_quote(LOG_TABLE)}")
        self.session.flush()
        return self.operation


@contextmanager
def record_operation(
    session: Session, kind: str, group_ids: list[int], summary: str
) -> Iterator[OperationJournal | None]:
    # Keep existing editing functional on other database engines; SQLite is the deployed backend.
    if session.get_bind().dialect.name != "sqlite":
        yield None
        return
    try:
        journal = OperationJournal(session, kind, group_ids, summary)
        yield journal
        journal.finish()
    except BaseException:
        session.rollback()  # Also rolls back the temporary trigger installation.
        raise


def _primary_key(columns: list[tuple[str, int]], row: dict) -> tuple[list[str], tuple]:
    keys = [name for name, number in sorted(columns, key=lambda item: item[1]) if number]
    if not keys or any(key not in row or row[key] is None for key in keys):
        raise UndoConflict("操作记录缺少有效主键")
    return keys, tuple(row[key] for key in keys)


def _where(keys: list[str]) -> str:
    return " AND ".join(f"{_quote(key)} IS ?" for key in keys)


def _current_row(
    connection, table: str, columns: list[tuple[str, int]], keys: list[str], values: tuple
):
    row = connection.exec_driver_sql(
        f"SELECT {_json_row(columns, '')} FROM {_quote(table)} WHERE {_where(keys)}", values
    ).first()
    return json.loads(row[0]) if row else None


def _assert_no_references(connection, parent: str, row: dict) -> None:
    tables = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    parent_pk = [
        name for name, number in sorted(_columns(connection, parent), key=lambda x: x[1]) if number
    ]
    for (child,) in tables:
        foreign_keys = connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({_quote(child)})"
        ).fetchall()
        groups = {}
        for fk in foreign_keys:
            if fk[2] == parent:
                groups.setdefault(fk[0], []).append(fk)
        for items in groups.values():
            items.sort(key=lambda fk: fk[1])
            keys = [fk[3] for fk in items]
            targets = [fk[4] or parent_pk[index] for index, fk in enumerate(items)]
            values = tuple(row.get(target) for target in targets)
            if any(value is None for value in values):
                continue
            found = connection.exec_driver_sql(
                f"SELECT 1 FROM {_quote(child)} WHERE {_where(keys)} LIMIT 1", values
            ).first()
            if found:
                raise UndoConflict("相关作品已有新的关联，无法安全撤销")


def undo_operation(session: Session, operation_id: int) -> CatalogOperation:
    if session.get_bind().dialect.name != "sqlite":
        raise UndoConflict("当前数据库暂不支持撤销")
    operation = session.get(CatalogOperation, operation_id)
    if operation is None:
        raise LookupError("整理记录不存在")
    result = session.execute(
        update(CatalogOperation)
        .where(CatalogOperation.id == operation_id, CatalogOperation.undone_at.is_(None))
        .values(undone_at=datetime.now(UTC))
    )
    if result.rowcount != 1:
        raise UndoConflict("此操作已经撤销")
    connection = session.connection()
    try:
        saved = operation.reference_state or {}
        parents = {name: set(values) for name, values in saved.get("parents", {}).items()}
        if set(parents) != {"works", "work_groups"} or _reference_state(
            connection, parents
        ) != saved.get("references"):
            raise UndoConflict("相关作品的关联已变化，无法安全撤销")
        for change in reversed(operation.changes):
            table, action = change["table"], change["action"]
            before, after = change["before"], change["after"]
            if table not in TRACKED or action not in {"INSERT", "UPDATE", "DELETE"}:
                raise UndoConflict("操作记录包含不支持的数据")
            columns = _columns(connection, table)
            expected = after if after is not None else before
            if not columns or set(expected) != {name for name, _ in columns}:
                raise UndoConflict("数据库结构已变化，无法安全撤销")
            keys, values = _primary_key(columns, expected)
            current = _current_row(connection, table, columns, keys, values)
            if action == "DELETE":
                if current is not None:
                    raise UndoConflict("原来的数据标识已被占用")
                names = list(before)
                connection.exec_driver_sql(
                    f"INSERT INTO {_quote(table)} ({','.join(map(_quote, names))}) "
                    f"VALUES ({','.join('?' for _ in names)})",
                    tuple(before[name] for name in names),
                )
            elif action == "INSERT":
                if current != after:
                    raise UndoConflict("相关数据已被修改，无法安全撤销")
                _assert_no_references(connection, table, after)
                connection.exec_driver_sql(
                    f"DELETE FROM {_quote(table)} WHERE {_where(keys)}", values
                )
            else:
                if current != after or set(before) != set(after):
                    raise UndoConflict("相关数据已被修改，无法安全撤销")
                names = list(before)
                connection.exec_driver_sql(
                    f"UPDATE {_quote(table)} SET {','.join(f'{_quote(name)}=?' for name in names)} "
                    f"WHERE {_where(keys)}",
                    tuple(before[name] for name in names) + values,
                )
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise UndoConflict("撤销会产生无效关联")
    except IntegrityError as exc:
        raise UndoConflict("原数据与当前记录冲突，无法安全撤销") from exc
    session.expire_all()
    return session.get(CatalogOperation, operation_id)
