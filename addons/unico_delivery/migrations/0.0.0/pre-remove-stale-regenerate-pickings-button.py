# -*- coding: utf-8 -*-
"""Remove stale sale-order buttons before view validation runs."""

import logging

_logger = logging.getLogger(__name__)

STALE_ACTION = "action_unico_regenerate_pickings"


def migrate(cr, version):
    if not version:
        return

    like_pattern = "%%%s%%" % STALE_ACTION
    cr.execute(
        """
        UPDATE ir_ui_view
           SET arch_db = REGEXP_REPLACE(
                   REGEXP_REPLACE(
                       arch_db::text,
                       '<button[^>]*action_unico_regenerate_pickings[^>]*/>',
                       '',
                       'g'
                   ),
                   '<button[^>]*action_unico_regenerate_pickings[^>]*>[^<]*</button>',
                   '',
                   'g'
               )::jsonb
         WHERE model = 'sale.order'
           AND arch_db::text LIKE %s
        RETURNING id, key, name
        """,
        [like_pattern],
    )
    cleaned_views = cr.fetchall()

    cr.execute(
        """
        SELECT id, key, name
          FROM ir_ui_view
         WHERE model = 'sale.order'
           AND active
           AND arch_db::text LIKE %s
        """,
        [like_pattern],
    )
    remaining_views = cr.fetchall()
    if remaining_views:
        cr.execute(
            "UPDATE ir_ui_view SET active = false WHERE id = ANY(%s)",
            [[view_id for view_id, _key, _name in remaining_views]],
        )

    if cleaned_views or remaining_views:
        _logger.info(
            "unico_delivery: removed stale %s button from %d sale.order view(s); "
            "deactivated %d remaining stale view(s)",
            STALE_ACTION,
            len(cleaned_views),
            len(remaining_views),
        )
