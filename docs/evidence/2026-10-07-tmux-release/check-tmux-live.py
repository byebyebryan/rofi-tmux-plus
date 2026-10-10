import argparse
import importlib.util
import json
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

sys.dont_write_bytecode = True


parser = argparse.ArgumentParser()
parser.add_argument('--host', required=True)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
spec = importlib.util.spec_from_loader('tmux_release_live', SourceFileLoader(
    'tmux_release_live', str(args.source / 'scripts/check-rofi-plus-live')))
suite = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = suite
spec.loader.exec_module(suite)
history_path = suite._history_path()
before = suite._history_snapshot(history_path)
with tempfile.TemporaryDirectory(prefix='tmux-plus-release-live-') as scratch:
    env = suite._clean_environment()
    env.update(XDG_CACHE_HOME=scratch+'/cache', XDG_STATE_HOME=scratch+'/state')
    ssh, tmux = suite._resolve('rofi-ssh-plus'), suite._resolve('rofi-tmux-plus')
    hosts, local, revision = suite._check_mesh(ssh, env)
    assert local == args.host
    suite._check_inventory(tmux, local, revision, env)
    suite._check_view_navigation('rofi-tmux-plus', tmux, env)
    suite._check_action_cycle('rofi-tmux-plus', tmux, env)
    initial = suite._render_callback(tmux, 0, env)
    assert suite._rofi_header(initial, 'prompt') in {'Tmux › All', 'Tmux › Local'}
    attached = suite._render_callback(tmux, 12, env, data=suite._rofi_header(initial, 'data'))
    assert suite._rofi_header(attached, 'prompt') == 'Tmux › Attached'
    opened = suite._render_callback(tmux, 12, env, data=suite._rofi_header(attached, 'data'))
    assert suite._rofi_header(opened, 'prompt') == 'Tmux › Open'
    reopened = suite._render_callback(tmux, 0, env)
    assert suite._rofi_header(reopened, 'prompt') == 'Tmux › Open'
    suite.LifecycleGate(tmux, env).run(hosts, revision)
    # Exercise real installed finite jobs and assert both clocks report completion.
    sys.path.insert(0, str(Path(tmux).resolve().parents[1]))
    from rofi_tmux_plus.config import load_config
    from rofi_tmux_plus.mesh_adapter import HostMeshAdapter
    from rofi_tmux_plus.picker_model import RemoteRefresh, ViewerRefresh, PickerModelService
    from rofi_tmux_plus.remote_cache import RemoteCache
    from rofi_tmux_plus.viewer_cache import ViewerObservationCache
    os.environ.update(XDG_CACHE_HOME=scratch+'/jobs-cache', XDG_STATE_HOME=scratch+'/jobs-state')
    config, adapter = load_config(), HostMeshAdapter()
    cache, viewers = RemoteCache(), ViewerObservationCache()
    owner = RemoteRefresh(config, cache, mesh_adapter=adapter, viewer_cache=viewers)
    viewer = ViewerRefresh(config, cache, mesh_adapter=adapter, viewer_cache=viewers)
    assert owner.run(revision) and owner.status(revision)['state'] == 'complete'
    assert viewer.run(revision) and viewer.status(revision)['state'] == 'complete'
    model = PickerModelService(config, cache=cache, viewer_cache=viewers, mesh_adapter=adapter).load(start_refresh=False).payload
    assert {host['hostId'] for host in model['hosts']} == set(hosts)
    assert all(host['status'] == 'ok' for host in model['hosts'])
    result = {'host': args.host, 'meshRevision': revision, 'hosts': hosts,
              'headlessNavigationActions': True, 'openAttachedViews': True, 'rememberedOpenView': True,
              'configuredRouteLifecycle': True, 'exactDisposableCleanup': True,
              'ownerJob': 'complete', 'viewerJob': 'complete', 'healthyInventory': True,
              'privateCacheAndPreferences': True}
suite._assert_history_unchanged(before, history_path)
result['sshHistoryPreserved'] = True
Path(f'/tmp/tmux-plus-release-live-{args.host}.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result))
