from app.jobs import record_usages


class _Node:
    def __init__(self, *, api=None, api_error=None, connected=True, started=True):
        self._api = api
        self._api_error = api_error
        self.connected = connected
        self.started = started
        self.usage_coefficient = 2

    @property
    def api(self):
        if self._api_error:
            raise self._api_error
        return self._api


def test_started_node_api_instances_skip_nodes_without_xray_api(monkeypatch):
    xray_api = object()
    sing_box_node = _Node(api_error=ConnectionError("Node does not expose Xray API"))
    xray_node = _Node(api=xray_api)

    monkeypatch.setattr(
        record_usages.xray,
        "nodes",
        {
            1: sing_box_node,
            2: xray_node,
            3: _Node(api=object(), connected=False),
            4: _Node(api=object(), started=False),
        },
    )

    api_instances, usage_coefficient = record_usages.get_started_node_api_instances()

    assert api_instances == {2: xray_api}
    assert usage_coefficient == {2: 2}
