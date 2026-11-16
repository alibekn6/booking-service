"""Post-run audit of the database: the acceptance check after a load test.

Each check is a query returning offending rows; an empty result means the invariant
holds. Raw SQL on purpose: an auditor should be able to read it without the ORM.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CHECKS: dict[str, str] = {
    # One seat - at most one owner.
    "seat_sold_twice": """
        SELECT seat_id FROM tickets GROUP BY seat_id HAVING count(*) > 1""",
    "sold_seat_without_its_ticket": """
        SELECT s.id FROM seats s
          LEFT JOIN tickets t ON t.seat_id = s.id AND t.order_id = s.order_id
         WHERE s.status = 'sold' AND t.id IS NULL""",
    "ticket_on_seat_not_sold_to_it": """
        SELECT t.id FROM tickets t JOIN seats s ON s.id = t.seat_id
         WHERE s.status <> 'sold' OR s.order_id IS DISTINCT FROM t.order_id""",
    "ticket_owner_mismatch": """
        SELECT t.id FROM tickets t
          JOIN orders o ON o.id = t.order_id
          JOIN seats s ON s.id = t.seat_id
         WHERE s.buyer_id IS DISTINCT FROM o.buyer_id""",
    # <= N seats per buyer per event, bought + live holds.
    "buyer_over_limit": """
        SELECT event_id, buyer_id, count(*) AS seats FROM seats
         WHERE buyer_id IS NOT NULL
           AND (status = 'sold' OR (status = 'held' AND held_until > now()))
         GROUP BY event_id, buyer_id HAVING count(*) > :max_seats""",
    # One order per hold, with exactly the held seats.
    "confirmed_hold_without_order": """
        SELECT h.id FROM holds h LEFT JOIN orders o ON o.hold_id = h.id
         WHERE h.status = 'confirmed' AND o.id IS NULL""",
    "order_ticket_count_mismatch": """
        SELECT o.id FROM orders o
         WHERE (SELECT count(*) FROM tickets t WHERE t.order_id = o.id)
            <> (SELECT count(*) FROM hold_seats hs WHERE hs.hold_id = o.hold_id)""",
    # Nothing confirmed at or after the deadline.
    "order_after_hold_deadline": """
        SELECT o.id FROM orders o JOIN holds h ON h.id = o.hold_id
         WHERE o.created_at >= h.expires_at""",
    # A live hold owns all of its seats or none.
    "partial_hold": """
        SELECT h.id FROM holds h
         WHERE h.status = 'active' AND h.expires_at > now()
           AND (SELECT count(*) FROM seats s WHERE s.hold_id = h.id AND s.status = 'held')
            <> (SELECT count(*) FROM hold_seats hs WHERE hs.hold_id = h.id)""",
    # No hold before the sale opened.
    "hold_before_sale_start": """
        SELECT h.id FROM holds h JOIN events e ON e.id = h.event_id
         WHERE h.created_at < e.sale_starts_at""",
    # A held seat must belong to a live, matching hold...
    "seat_held_by_dead_hold": """
        SELECT s.id FROM seats s JOIN holds h ON h.id = s.hold_id
         WHERE s.status = 'held' AND s.held_until > now()
           AND (h.status <> 'active' OR h.expires_at <> s.held_until
                OR h.buyer_id <> s.buyer_id)""",
    # ...and expired holds must actually be cleaned up (sweeper alive).
    "expired_hold_not_released": """
        SELECT s.id FROM seats s
         WHERE s.status = 'held' AND s.held_until < now() - make_interval(secs => :grace_s)""",
}

SUMMARY = """
SELECT e.id AS event_id, e.name,
       count(s.id)                                                    AS seats,
       count(s.id) FILTER (WHERE s.status = 'sold')                   AS sold,
       count(s.id) FILTER (WHERE s.status = 'held' AND s.held_until > now()) AS held,
       count(s.id) FILTER (WHERE s.status = 'available'
                            OR (s.status = 'held' AND s.held_until <= now())) AS available,
       (SELECT count(*) FROM orders o WHERE o.event_id = e.id)        AS orders,
       (SELECT count(DISTINCT buyer_id) FROM orders o WHERE o.event_id = e.id) AS buyers
  FROM events e LEFT JOIN seats s ON s.event_id = e.id
 GROUP BY e.id ORDER BY e.created_at
"""


@dataclass
class AuditReport:
    violations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(self.violations.values())


async def audit(conn: AsyncConnection, *, max_seats: int, grace_s: float = 60) -> AuditReport:
    params = {"max_seats": max_seats, "grace_s": grace_s}
    report = AuditReport()
    for name, sql in CHECKS.items():
        stmt = text(sql)
        bound = {k: v for k, v in params.items() if f":{k}" in sql}
        rows = (await conn.execute(stmt, bound)).mappings().all()
        report.violations[name] = [dict(r) for r in rows[:20]]
    report.events = [dict(r) for r in (await conn.execute(text(SUMMARY))).mappings().all()]
    return report
