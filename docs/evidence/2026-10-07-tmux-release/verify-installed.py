import argparse
import hashlib
import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def preferences():
    directory = Path.home() / '.local/state/rofi-tmux-plus'
    return {
        str(path.relative_to(directory)): digest(path.read_bytes())
        for path in sorted(directory.rglob('*')) if path.is_file() and not path.is_symlink()
    }


def pilot_links():
    names = ['.local/bin/agent-observer', '.local/bin/agent-observer-write',
             '.local/bin/rofi-agent-plus', '.local/bin/rofi-agent-plus-rofi',
             '.config/rofi/scripts/agent-plus']
    return {name: str((Path.home() / name).resolve(strict=True)) for name in names}


SOURCE_INPUTS = {'.chezmoiexternal.toml', '.chezmoiignore',
                 'dot_config/niri/dms/binds.kdl', 'dot_local/bin/symlink_rofi-tmux-plus-rofi'}


def worktree(root, undo_inputs=False):
    files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached',
                                     '--others', '--exclude-standard']).split(b'\0')
    entries = {}
    for name in sorted(set(name for name in files if name)):
        path = root / name.decode()
        if undo_inputs and name.decode() == 'dot_local/bin/symlink_rofi-tmux-plus-rofi':
            assert path.read_text() == '../share/rofi/extensions/tmux-plus/bin/rofi-tmux-plus-rofi\n'
            continue
        if path.is_symlink():
            entries[name.decode()] = ['link', os.readlink(path)]
        elif path.is_file():
            data = path.read_bytes()
            if undo_inputs and name.decode() == '.chezmoiexternal.toml':
                data = data.replace(b'407ae58ba422ba88fed7da2f9d845ff274830f0e', b'5b02f84fc0e18215426c652acfa942cdbae4cbcb')
                data = data.replace(b'fd448eb2202de9708976b52290510770638eba2f8847dacc185730650ffcc470', b'03b61a1ef4a948396520b81118e7121b452878a3405c2868fca2ad39df297f67')
            elif undo_inputs and name.decode() == '.chezmoiignore':
                data = data.replace(b'.local/bin/rofi-tmux-plus-rofi\n', b'')
            elif undo_inputs and name.decode() == 'dot_config/niri/dms/binds.kdl':
                data = data.replace(b'spawn "rofi-tmux-plus-rofi"', b'spawn "rofi"')
            entries[name.decode()] = ['file', digest(data), stat.S_IMODE(path.stat().st_mode)]
        else:
            entries[name.decode()] = ['missing']
    return {
        'head': subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip(),
        'status': subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1'], text=True),
        'contentDigest': digest(json.dumps(entries, sort_keys=True).encode()),
    }


parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=['before', 'verify'])
parser.add_argument('--host', required=True)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--preserve-worktree', type=Path)
parser.add_argument('--source-inputs-updated', action='store_true')
args = parser.parse_args()
record = Path('/tmp') / f'tmux-plus-release-before-{args.host}.json'
if args.mode == 'before':
    result = {'pilotLinks': pilot_links(), 'preferences': preferences()}
    if args.preserve_worktree:
        result['worktree'] = worktree(args.preserve_worktree)
    record.write_text(json.dumps(result))
    record.chmod(0o600)
    print(json.dumps({'host': args.host, 'snapshot': str(record)}))
else:
    baseline = json.loads(record.read_text())
    assert baseline['pilotLinks'] == pilot_links(), 'pilot selection changed'
    assert baseline['preferences'] == preferences(), 'ordinary preferences changed'
    if args.preserve_worktree:
        current = worktree(args.preserve_worktree, undo_inputs=args.source_inputs_updated)
        previous = dict(baseline['worktree'])
        if args.source_inputs_updated:
            for value in (previous, current):
                value['status'] = '\n'.join(line for line in value['status'].splitlines() if line[3:] not in SOURCE_INPUTS)
        assert previous == current, 'unrelated checkout content changed'
    manifest = json.loads(Path('/tmp/tmux-plus-release-manifest.json').read_text())
    installed = Path.home() / '.local/share/rofi/extensions/tmux-plus'
    for name, expected in manifest['files'].items():
        path = installed / name
        assert not path.is_symlink() and path.is_file(), name
        assert digest(path.read_bytes()) == expected['sha256'], name
        assert bool(path.stat().st_mode & 0o111) == expected['executable'], name
    assert tomllib.loads((installed / 'pyproject.toml').read_text())['project']['version'] == manifest['version']
    links = {'.local/bin/rofi-tmux-plus': 'bin/rofi-tmux-plus',
             '.local/bin/rofi-tmux-plus-rofi': 'bin/rofi-tmux-plus-rofi',
             '.config/rofi/scripts/tmux-plus': 'bin/rofi-tmux-plus'}
    for name, target in links.items():
        assert (Path.home() / name).is_symlink()
        assert (Path.home() / name).resolve(strict=True) == installed / target, name
    assert (Path.home() / '.config/niri/dms/binds.kdl').read_bytes() == (args.source / 'dot_config/niri/dms/binds.kdl').read_bytes()
    result = {'host': args.host, 'sourceRevision': manifest['sourceRevision'], 'version': manifest['version'],
              'archiveSha256': manifest['archiveSha256'], 'trackedFilesVerified': len(manifest['files']),
              'executableModesVerified': True, 'tmuxLinksVerified': True, 'managedBindingVerified': True,
              'pilotLinks': pilot_links(), 'ordinaryPreferencesPreserved': True,
              'dirtyCheckoutPreserved': bool(args.preserve_worktree),
              'durableTmuxSourceInputsUpdated': args.source_inputs_updated}
    Path(f'/tmp/tmux-plus-release-installed-{args.host}.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
