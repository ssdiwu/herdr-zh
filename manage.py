#!/usr/bin/env python3
"""Herdr 中文定制包：生成词典副本、检查、安装、回退和补丁监听。"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
VERSION = "0.1.15"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def current(path):
    return path.read_bytes() if path.exists() else None


def fingerprint(data):
    return digest(data) if data is not None else None


def target(home, rel):
    """只允许包声明的相对位置，拒绝软链接目标。"""
    rel = Path(rel)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError(f"不合法的相对路径：{rel}")
    result = home / rel
    if any(p.is_symlink() for p in [result, *result.parents] if p != home.parent):
        raise ValueError(f"拒绝写入软链接：{result}")
    if not result.resolve().is_relative_to(home.resolve()):
        raise ValueError(f"路径超出应用目录：{result}")
    return result


def atomic(path, data, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    if current(path) != expected:
        raise RuntimeError(f"文件在检查后改变，已停止：{path}")
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if current(path) != expected:
            raise RuntimeError(f"文件在写入前改变，已停止：{path}")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def operation_lock(home):
    path = target(home, '.herdr-zh/operation.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def encoded(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode()


def build():
    data = (ROOT / 'translations/dict.js').read_bytes()
    for folder in sorted((ROOT / 'mods').iterdir()):
        path = folder / 'i18n.js'
        if current(path) != data:
            atomic(path, data, current(path))
    print('词典副本已与唯一维护源同步。')


def rules():
    return {f'frame/{p.stem}/main.js': read_json(p)
            for p in sorted((ROOT / 'translations/frame').glob('*.json'))}


def translate(text, edits, name):
    missing = []
    for e in edits:
        old, new = e['from'], e['to']
        if not old or old == new:
            continue
        if old in text and not (old in new and new in text):
            text = text.replace(old, new)
        elif new not in text:
            missing.append(old)
    if missing:
        raise RuntimeError(f"{name} 有 {len(missing)} 条失配规则：" + repr(missing))
    return text


def require_version():
    r = subprocess.run(['herdr-app', '--version'], capture_output=True, text=True, check=True, timeout=10)
    if r.stdout.strip() != f'herdr-app {VERSION}':
        raise RuntimeError(f"仅核验过 Herdr {VERSION}，当前为 {r.stdout.strip()}；请先检查升级差异。")


def frame_plan(home):
    plan = {}
    for rel, edits in rules().items():
        path = target(home, rel)
        old = path.read_bytes()
        new = translate(old.decode(), edits, rel).encode()
        if old != new:
            plan[rel] = (old, new)
    return plan


def payload():
    result = {}
    for folder in ['layouts', 'mods']:
        for path in sorted((ROOT / folder).rglob('*')):
            if path.is_symlink():
                raise RuntimeError(f'包中不允许软链接：{path}')
            if path.is_file() and not path.name.startswith('.') and path.suffix in ['.js', '.json', '.svg', '.png'] and path.name != 'jsconfig.json':
                result[str(path.relative_to(ROOT))] = path.read_bytes()
    canonical = (ROOT / 'translations/dict.js').read_bytes()
    for rel in result:
        if rel.endswith('/i18n.js') and result[rel] != canonical:
            raise RuntimeError('词典副本过期，请先运行 build。')
    return result


def merge_json(home, rel, patch, accepted, prior):
    path = target(home, rel)
    old = current(path)
    value = json.loads(old or b'{}')
    allowed = accepted.get(rel) == fingerprint(old) or prior.get(rel) == fingerprint(old)
    def merge(dest, src, prefix=''):
        for key, val in src.items():
            if isinstance(val, dict):
                if key not in dest:
                    dest[key] = {}
                if not isinstance(dest[key], dict):
                    raise RuntimeError(f'{rel} 的 {prefix}{key} 结构冲突')
                merge(dest[key], val, prefix + key + '.')
            elif key not in dest or dest[key] == val or allowed:
                dest[key] = val
            else:
                raise RuntimeError(f'{rel} 已使用 {prefix}{key}，拒绝覆盖现有值。')
    merge(value, patch)
    # 无语义变化时保留原字节，避免碰触无关配置格式。
    return old, old if value == json.loads(old or b'{}') else encoded(value)


def install_plan(home, baseline=None, personal=False, frames=False):
    accepted = read_json(baseline) if baseline else {}
    state = target(home, '.herdr-zh/installed.json')
    prior = read_json(state).get('files', {}) if state.exists() else {}
    plan = {}
    for rel, data in payload().items():
        old = current(target(home, rel))
        if old == data:
            continue
        if old is not None and fingerprint(old) not in [accepted.get(rel), prior.get(rel)]:
            raise RuntimeError(f'同名文件已有未纳管改动，拒绝覆盖：{rel}')
        plan[rel] = (old, data)
    configs = {'keys.json': read_json(ROOT / 'keys.json')}
    if personal:
        configs['settings.json'] = read_json(ROOT / 'presets/507-settings.json')
        configs['keys.json'] = read_json(ROOT / 'presets/507-keys.json')
    for rel, patch in configs.items():
        pair = merge_json(home, rel, patch, accepted, prior)
        if pair[0] != pair[1]:
            plan[rel] = pair
    if frames:
        plan.update(frame_plan(home))
    return plan


def apply_plan(home, plan):
    state = target(home, '.herdr-zh/installed.json')
    state_old = current(state)
    files = json.loads(state_old or b'{}').get('files', {})
    if not plan:
        print('当前文件已一致，无需写入。')
        return None
    # 完整预检通过后才备份或写入。
    for rel, (old, _) in plan.items():
        if current(target(home, rel)) != old:
            raise RuntimeError(f'预检期间发生修改：{rel}')
    receipt = target(home, '.herdr-zh/backups/' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    receipt.mkdir(parents=True)
    entries = {}
    for rel, (old, new) in plan.items():
        if old is not None:
            dest = receipt / 'before' / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(old)
        entries[rel] = {'before': fingerprint(old), 'after': digest(new)}
    record = receipt / 'receipt.json'
    atomic(record, encoded({'home': str(home), 'files': entries, 'applied': []}), None)
    applied = []
    try:
        for rel, (old, new) in plan.items():
            atomic(target(home, rel), new, old)
            applied.append(rel)
            atomic(record, encoded({'home': str(home), 'files': entries, 'applied': applied}), record.read_bytes())
    except Exception:
        print(f'部分写入；恢复记录：{record}', file=sys.stderr)
        raise
    files.update({rel: digest(data) for rel, data in payload().items()})
    files.update({rel: digest(new) for rel, (_, new) in plan.items()})
    atomic(state, encoded({'files': files, 'receipt': str(record)}), state_old)
    print(f'已写入 {len(plan)} 个文件；回退记录：{record}')
    return record


def rollback(home, record):
    data = read_json(record)
    if Path(data['home']).resolve() != home:
        raise RuntimeError('回退记录属于另一个应用目录。')
    state = target(home, '.herdr-zh/installed.json')
    state_before = current(state)
    plan = []
    for rel, item in data['files'].items():
        dest = target(home, rel)
        old = current(dest)
        if fingerprint(old) == item['before']:
            continue
        if fingerprint(old) != item['after']:
            raise RuntimeError(f'安装后已有其他修改，拒绝回退覆盖：{rel}')
        before = None
        if item['before'] is not None:
            before = target(record.parent / 'before', rel).read_bytes()
            if digest(before) != item['before']:
                raise RuntimeError(f'备份内容校验失败：{rel}')
        plan.append((dest, old, before))
    for dest, old, before in plan:
        if before is None:
            if current(dest) != old:
                raise RuntimeError(f'回退期间发生修改：{dest}')
            dest.unlink()
        else:
            atomic(dest, before, old)
    # 清理已回退文件的管理指纹；其他安装记录保留。
    old = state_before
    if old:
        value = json.loads(old)
        for rel in data['files']:
            value.get('files', {}).pop(rel, None)
        atomic(state, encoded(value), old)
    print(f'已回退 {len(plan)} 个文件。')


def check(home, native=False):
    payload()
    planned = frame_plan(home)
    print(f'5 个内置页面的翻译规则全部可匹配；{len(planned)} 个页面待应用。')
    if native:
        require_version()
        for folder in sorted((ROOT / 'mods').iterdir()):
            subprocess.run(['herdr-app', 'check', str(folder)], check=True, cwd=home,
                           env={**os.environ, 'HERDR_APP_HOME': str(home)}, timeout=30)
        print('20 个模块的原生检查通过。')


def watch(home):
    # 所有本包监听入口共享同一把锁；重启先检查一次，不漏掉启动前的重写。
    lock = target(home, '.herdr-zh/watch.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有中文补丁监听实例。')
        previous = None
        error = None
        while True:
            stamp = tuple((rel, fingerprint(current(target(home, rel)))) for rel in rules())
            if stamp != previous:
                try:
                    require_version()
                    with operation_lock(home):
                        plan = frame_plan(home)
                        for rel, (old, new) in plan.items():
                            atomic(target(home, rel), new, old)
                    expected = dict(stamp)
                    expected.update({rel: digest(new) for rel, (_, new) in plan.items()})
                    previous = tuple(expected.items())
                    if plan:
                        print(f'补汉化 {len(plan)} 个页面。', flush=True)
                    error = None
                except Exception as exc:
                    if str(exc) != error:
                        print(f'补汉化暂停：{exc}', file=sys.stderr, flush=True)
                        error = str(exc)
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'check', 'install', 'rollback', 'watch', 'patch'])
    parser.add_argument('--app-home', type=Path, default=Path.home() / '.config/herdr-app')
    parser.add_argument('--baseline', type=Path, help='接管已有定制时的只读 SHA256 基准清单')
    parser.add_argument('--personal', action='store_true', help='同时合并 507 的个人偏好预设')
    parser.add_argument('--frames', action='store_true', help='同时应用非官方的内置页面中文补丁')
    parser.add_argument('--native', action='store_true', help='运行 herdr-app check 原生检查')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    home = args.app_home.expanduser().resolve()
    if args.command == 'build':
        build()
    elif args.command == 'check':
        check(home, args.native)
    elif args.command == 'install':
        require_version()
        if args.dry_run:
            plan = install_plan(home, args.baseline, args.personal, args.frames)
            print(json.dumps(list(plan), ensure_ascii=False, indent=2))
        else:
            with operation_lock(home):
                plan = install_plan(home, args.baseline, args.personal, args.frames)
                apply_plan(home, plan)
    elif args.command == 'patch':
        require_version()
        with operation_lock(home):
            apply_plan(home, frame_plan(home))
    elif args.command == 'rollback':
        if not args.receipt:
            parser.error('rollback 必须指定 --receipt')
        with operation_lock(home):
            rollback(home, args.receipt.resolve())
    else:
        watch(home)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f'失败：{exc}', file=sys.stderr)
        sys.exit(1)
