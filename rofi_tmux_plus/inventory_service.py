"""Fresh Tmux Session v1 facade; native/network/desktop jobs belong to Observer."""

from .inputs import validate_user_option
from .observer_client import client_error, observer_api


class InventoryService:
    def __init__(self, config, *, mesh_adapter=None, direct_inventory=None):
        self._config = config
        self._mesh = mesh_adapter
        self._direct = direct_inventory

    def inventory(self, *, requested_hosts, mesh_revision, panes, option_names, with_viewers=False):
        options = tuple(dict.fromkeys(validate_user_option(name) for name in option_names))
        try:
            api = observer_api() if self._direct is None else None
            direct = self._direct or api.direct.DirectInventory(mesh=self._mesh)
            parameters = {
                "requested_hosts": requested_hosts,
                "mesh_revision": mesh_revision,
                "panes": panes,
                "option_names": options,
            }
            if with_viewers:
                parameters.update(
                    with_viewers=True,
                    desktop_config=api.desktop_config.DesktopConfig(terminal=self._config.terminal)
                    if api is not None
                    else None,
                )
            response = direct.inventory(**parameters)
        except Exception as error:
            raise client_error(error) from error
        for row in response["hosts"]:
            if row["status"] != "ok":
                row["sessions"] = []
        return response
