"""A tiny in-memory PostgREST stand-in for unit tests (no network, no database).

Understands the filters the app sends: ``col=eq.v``, ``col=in.(1,2)``, ``order=a.desc,b.asc``,
``limit``/``offset`` and ``select`` (column pruning), plus ``POST /rest/v1/<table>`` inserts
and ``POST /rest/v1/rpc/<name>`` calls whose responses are supplied by the test.
This proves the *application's* query scoping and error handling; it does NOT prove anything
about the real database (see backend/tests/pg for that).
"""

import httpx


class FakeSupabase:
    def __init__(self, **tables):
        self.tables = {name: [dict(r) for r in rows] for name, rows in tables.items()}
        self.calls = []  # (method, path, params, json_body)
        self.rpc = {}  # name -> callable(payload) -> httpx.Response | dict
        self.rpc_calls = []  # (name, payload)

    # -- query helpers ----------------------------------------------------------------
    @staticmethod
    def _match(row, column, expr):
        value = row.get(column)
        if isinstance(value, bool):  # PostgREST spells booleans in lower case
            value = str(value).lower()
        if expr.startswith("eq."):
            return str(value) == expr[3:]
        if expr.startswith("neq."):
            return str(value) != expr[4:]
        if expr.startswith("in.(") and expr.endswith(")"):
            return str(value) in set(expr[4:-1].split(","))
        raise AssertionError(f"unsupported filter {column}={expr}")

    def select(self, table, params):
        rows = list(self.tables.get(table, []))
        reserved = {"select", "order", "limit", "offset"}
        for column, expr in params.items():
            if column not in reserved:
                rows = [r for r in rows if self._match(r, column, expr)]
        for spec in reversed([s for s in params.get("order", "").split(",") if s]):
            column, _, direction = spec.partition(".")
            rows.sort(key=lambda r: (r.get(column) is None, r.get(column)), reverse=direction == "desc")
        offset = int(params.get("offset", 0))
        limit = int(params["limit"]) if "limit" in params else None
        rows = rows[offset: offset + limit if limit is not None else None]
        columns = params.get("select", "*")
        if columns != "*":
            wanted = columns.split(",")
            rows = [{c: r.get(c) for c in wanted} for r in rows]
        return rows

    # -- the patched replacement for app.main._request_supabase -------------------------------
    async def request(self, path, **kw):
        method = kw.get("method", "GET")
        params = kw.get("params") or {}
        body = kw.get("json_body")
        self.calls.append((method, path, params, body))
        if path.startswith("/rest/v1/rpc/"):
            name = path.rsplit("/", 1)[1]
            self.rpc_calls.append((name, body))
            result = self.rpc[name](body)
            return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
        table = path.removeprefix("/rest/v1/")
        if method == "POST":
            self.tables.setdefault(table, []).append(dict(body))
            return httpx.Response(201)
        return httpx.Response(200, json=self.select(table, params))

    # -- assertions -------------------------------------------------------------------------
    def read_tables(self):
        return [p.removeprefix("/rest/v1/") for m, p, _, _ in self.calls if m == "GET"]

    def audits(self):
        return [b for m, p, _, b in self.calls if m == "POST" and p.endswith("/audit_logs")]
