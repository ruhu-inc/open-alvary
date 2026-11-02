"""What the workbench's Traffic section reads: the website's daily totals (api.traffic, TRF-04) and how
developers come to use the API, through the workbench's limited database role.

Only totals are read, and only what is already summed: a report takes the same time however many records
there are and however busy the site has been, since the most read records are worked out once a day.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

from rights.database import PolicyNamespace

RANGES = (7, 30, 90, 365)
LISTED = 50
# The daily lines: (name, metric, label); no label adds up all the metric's labels.
SERIES = (
    ("arrivals", "arrivals", None),
    ("visitors", "visitors", "all"),
    ("searches", "searches", "all"),
    ("none_found", "searches", "none_found"),
    ("records", "records", "views"),
    ("texts", "records", "texts"),
    ("downloads", "downloads", None),
    ("bots", "bots", None),
)
LISTS = {
    "landing": "arrivals",
    "country": "people.country",
    "device": "people.device",
    "referrer": "people.referrer",
    "searched_country": "searches.jurisdiction",
    "opened_position": "searches.opened",
    "read_country": "records.jurisdiction",
    "downloads": "downloads",
    "bots": "bots",
    "lost": "lost",
}


def report(engine: Engine, namespace: PolicyNamespace, days: int = 30, today: date | None = None) -> dict:
    days = days if days in RANGES else 30
    today = today or datetime.now(UTC).date()
    since = today - timedelta(days=days - 1)
    span = {"since": since, "today": today}
    with engine.connect() as connection:

        def t(name: str) -> str:
            return namespace.qualified(connection, "accounts", name)

        def rows(sql: str, **parameters) -> list[dict]:
            return [dict(row) for row in connection.execute(text(sql), parameters).mappings()]

        totals = t("traffic_day")
        by_day: dict[str, dict] = {}
        for row in rows(
            f"SELECT day, metric, label, count FROM {totals} WHERE day BETWEEN :since AND :today"
            " AND metric = ANY(:metrics)",
            metrics=sorted({metric for _, metric, _ in SERIES}),
            **span,
        ):
            line = by_day.setdefault(str(row["day"]), {})
            for name, metric, label in SERIES:
                if row["metric"] == metric and label in (None, row["label"]):
                    line[name] = line.get(name, 0) + int(row["count"])
        daily = []
        for offset in range(days):
            day = str(since + timedelta(days=offset))
            daily.append({"day": day, **{name: by_day.get(day, {}).get(name, 0) for name, _, _ in SERIES}})

        lists: dict[str, list] = {name: [] for name in LISTS}
        names = {metric: name for name, metric in LISTS.items()}
        for row in rows(
            "SELECT metric, label, count FROM (SELECT metric, label, sum(count) AS count, row_number() OVER"
            " (PARTITION BY metric ORDER BY sum(count) DESC, label) AS place"
            f" FROM {totals} WHERE day BETWEEN :since AND :today AND metric = ANY(:metrics)"
            " GROUP BY metric, label) AS ranked WHERE place <= :listed ORDER BY metric, place",
            metrics=sorted(names),
            listed=LISTED,
            **span,
        ):
            lists[names[row["metric"]]].append({"label": row["label"], "count": int(row["count"])})

        window = "7d" if days == 7 else "30d"
        top = rows(
            f"SELECT source_id, views, computed_at FROM {t('traffic_top')} WHERE period = :period"
            " ORDER BY rank LIMIT :listed",
            period=window,
            listed=LISTED,
        )

        people, organisations, keys, usage = t("person"), t("organisation"), t("api_key"), t("usage_day")
        apps = t("client_grant")
        developers = rows(
            "SELECT"
            f" (SELECT count(*) FROM {people} WHERE state <> 'closed') AS people,"
            f" (SELECT count(*) FROM {people} WHERE created_at >= :opened) AS people_new,"
            f" (SELECT count(*) FROM {organisations}) AS organisations,"
            f" (SELECT count(*) FROM {keys} WHERE created_at >= :opened) AS keys_new,"
            f" (SELECT count(*) FROM {keys} WHERE created_at >= :opened AND last_used_at IS NOT NULL)"
            " AS keys_new_used,"
            f" (SELECT count(*) FROM {apps} WHERE created_at >= :opened) AS apps_new,"
            f" (SELECT count(DISTINCT credential) FROM {usage} WHERE day > :week) AS credentials_week,"
            f" (SELECT count(DISTINCT organisation_id) FROM {usage} WHERE day > :week) AS organisations_week,"
            f" (SELECT coalesce(sum(requests), 0) FROM {usage} WHERE day BETWEEN :since AND :today"
            " AND class = 'rest') AS requests_rest,"
            f" (SELECT coalesce(sum(requests), 0) FROM {usage} WHERE day BETWEEN :since AND :today"
            " AND class = 'mcp') AS requests_mcp,"
            f" (SELECT coalesce(sum(requests), 0) FROM {usage} WHERE day BETWEEN :since AND :today"
            " AND class = 'bulk') AS requests_bulk",
            opened=datetime(since.year, since.month, since.day, tzinfo=UTC),
            week=today - timedelta(days=7),
            **span,
        )[0]

    sums = {name: sum(line[name] for line in daily) for name, _, _ in SERIES}
    return {
        "days": days,
        "since": str(since),
        "today": str(today),
        "daily": daily,
        "totals": sums,
        "lists": lists,
        "top_records": {
            "window": window,
            "computed_at": top[0]["computed_at"].isoformat() if top else None,
            "items": [{"source_id": row["source_id"], "views": int(row["views"])} for row in top],
        },
        "developers": {name: int(value) for name, value in developers.items()},
    }
