"""DRF permissions for LORA.

The manager/agent role split was removed — one trusted user type, gated by
authentication only. `IsManager` / `IsAgentOrManager` are kept as aliases of
`IsAuthenticated` so existing `permission_classes` lists keep working without
edits; there is no role check any more.
"""

from rest_framework import permissions

IsAgentOrManager = permissions.IsAuthenticated
IsManager = permissions.IsAuthenticated
