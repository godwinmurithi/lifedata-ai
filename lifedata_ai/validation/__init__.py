"""Write-action validation (spec §6).

Nothing the model proposes reaches Superset without passing through this
package, in the order spec §6 lays down: jsonschema first, then the policy
rules (viz allowlist, no adhoc SQL, geography, theme allowlist, risk), then the
semantic layout compiler.

This module intentionally imports nothing. `validator.py` and `layout.py` are
owned by a separate work stream and land later; keeping the package `__init__`
empty means their arrival cannot break importers of the schemas, and there is
no import cycle to unpick when they do land. Import the concrete pieces from
their own modules::

    from lifedata_ai.validation.schemas import ACTION_SCHEMAS, get_schema
"""

from __future__ import annotations
