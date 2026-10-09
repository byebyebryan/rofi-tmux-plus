"""Lazy Tmux Session v1 action facade; no action implementation is loaded to browse."""

from .observer_client import client_error, observer_api

_OPERATIONS = frozenset({"create", "open", "rename", "kill", "viewers", "close_viewer"})


class LifecycleService:
    def __init__(self, config, **dependencies):
        self._config = config
        self._dependencies = dependencies
        self._client = None

    def __getattr__(self, name):
        if name not in _OPERATIONS:
            raise AttributeError(name)

        def execute(*args, **kwargs):
            try:
                if self._client is None:
                    api = observer_api().actions
                    config = api.ActionConfig(
                        terminal=self._config.terminal,
                        attach_timeout_seconds=self._config.attach_timeout_seconds,
                    )
                    self._client = api.LifecycleService(config, **self._dependencies)
                return getattr(self._client, name)(*args, **kwargs)
            except Exception as error:
                raise client_error(error) from error

        return execute


class ActionService:
    """Picker intent crosses C5; legacy CLI argv retains its separate facade."""

    def __init__(self, config):
        self._config = config
        self._client = None

    def _execute(self, operation, host, revision, generation, session_id, created_at, name):
        import uuid

        request = {
            "protocol": "tmux-observer.action.v1",
            "schemaVersion": 1,
            "requestId": uuid.uuid4().hex,
            "operation": operation,
            "hostId": host,
            "meshRevision": revision,
            "sessionRef": {
                "hostId": host,
                "serverGeneration": generation,
                "sessionId": session_id,
                "createdAt": created_at,
            },
            "guards": {"expectedName": name, "requiredOptions": {}},
            "parameters": {"viewerPolicy": "reuse_unique"} if operation == "open" else {},
        }
        try:
            api = observer_api()
            api.action_contract.validate_action_request(request)
            if self._client is None:
                config = api.actions.ActionConfig(
                    terminal=self._config.terminal,
                    attach_timeout_seconds=self._config.attach_timeout_seconds,
                )
                self._client = api.actions.ActionClient(config)
            result = api.action_contract.validate_action_result(
                self._client.execute(request), request=request
            )
            if not result["ok"]:
                error = result["error"]
                from .errors import ContractError

                raise ContractError(error["code"], error["message"], host)
            return result["legacyResult"]
        except Exception as error:
            raise client_error(error) from error

    def open(self, host, revision, generation, session_id, created_at, expected_name=None):
        return self._execute(
            "open", host, revision, generation, session_id, created_at, expected_name
        )

    def kill(self, host, revision, generation, session_id, created_at, expected_name):
        return self._execute(
            "kill", host, revision, generation, session_id, created_at, expected_name
        )
