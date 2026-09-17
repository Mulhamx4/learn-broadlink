"""
Local web server for learning Broadlink IR codes into SmartIR device files.

Supports multiple devices of different platforms (climate, media_player, fan,
light) in a single project, and exports them either as one combined project
file or as a zip of SmartIR-ready per-device files.

Run:  python server.py
Then open http://127.0.0.1:8777 in your browser.
"""
import base64
import copy
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile

import broadlink
from broadlink.exceptions import ReadError, StorageError
from flask import Flask, jsonify, request, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_PATH = os.path.join(BASE_DIR, 'devices.json')
LEGACY_PATH = os.path.join(BASE_DIR, 'smartir.json')
TEMPLATE_PATH = os.path.join(BASE_DIR, 'template.json')
LEARN_TIMEOUT = 30

app = Flask(__name__, static_folder=None)

_lock = threading.Lock()
_device = None
_device_info = None


# ============================================================ platform specs
#
# Field names below are taken from the SmartIR component sources:
#   climate.py       operation -> [preset] -> fan -> swing -> temperature
#   media_player.py  off/on/previousChannel/nextChannel/volumeDown/volumeUp/
#                    mute/sources
#   fan.py           on/off/oscillate, commands[direction][speed]
#   light.py         on/off/night/brighten/dim/colder/warmer,
#                    commands[brightness][level], commands[colorTemperature][k]

T_TEXT, T_NUM, T_LIST, T_BOOL = 'text', 'number', 'list', 'bool'

COMMON_FIELDS = [
    {'key': 'manufacturer', 'type': T_TEXT, 'label': 'الشركة المصنّعة'},
    {'key': 'supportedModels', 'type': T_LIST, 'label': 'الموديلات المدعومة'},
]

PLATFORMS = {
    'climate': {
        'label': 'مكيّف',
        'icon': '❄',
        'fields': COMMON_FIELDS + [
            {'key': 'minTemperature', 'type': T_NUM, 'label': 'أدنى حرارة', 'default': 16},
            {'key': 'maxTemperature', 'type': T_NUM, 'label': 'أعلى حرارة', 'default': 30},
            {'key': 'precision', 'type': T_NUM, 'label': 'دقة الخطوة', 'default': 1},
            {'key': 'operationModes', 'type': T_LIST, 'label': 'أوضاع التشغيل',
             'default': ['cool', 'heat', 'dry', 'fan_only']},
            {'key': 'presetModes', 'type': T_LIST, 'label': 'أوضاع الإعداد (اختياري)', 'default': []},
            {'key': 'fanModes', 'type': T_LIST, 'label': 'سرعات المروحة',
             'default': ['auto', 'low', 'medium', 'high']},
            {'key': 'swingModes', 'type': T_LIST, 'label': 'أوضاع التوجيه', 'default': ['auto']},
            {'key': 'hasOn', 'type': T_BOOL, 'label': 'يملك زر تشغيل منفصل', 'default': False},
        ],
    },
    'media_player': {
        'label': 'تلفاز / مشغّل',
        'icon': '📺',
        'fields': COMMON_FIELDS + [
            {'key': 'sources', 'type': T_LIST, 'label': 'المصادر (HDMI1, HDMI2, ...)',
             'default': ['HDMI 1', 'HDMI 2']},
        ],
    },
    'fan': {
        'label': 'مروحة',
        'icon': '🌀',
        'fields': COMMON_FIELDS + [
            {'key': 'speed', 'type': T_LIST, 'label': 'السرعات',
             'default': ['low', 'medium', 'high']},
            {'key': 'directions', 'type': T_LIST, 'label': 'الاتجاهات', 'default': ['forward']},
            {'key': 'hasOscillate', 'type': T_BOOL, 'label': 'يملك زر تدوير (oscillate)',
             'default': True},
        ],
    },
    'light': {
        'label': 'إضاءة',
        'icon': '💡',
        'fields': COMMON_FIELDS + [
            {'key': 'brightness', 'type': T_LIST, 'label': 'مستويات السطوع (اختياري)', 'default': []},
            {'key': 'colorTemperature', 'type': T_LIST,
             'label': 'درجات حرارة اللون بالكلفن (اختياري)', 'default': []},
            {'key': 'hasStepped', 'type': T_BOOL,
             'label': 'يملك أزرار تدرّج (أفتح/أغمق)', 'default': True},
        ],
    },
}

# Keys that describe the UI only and must not be written into an exported
# SmartIR file.
UI_ONLY_KEYS = {'hasOn', 'hasOscillate', 'hasStepped', 'directions'}


def default_config(platform):
    cfg = {
        'manufacturer': '',
        'supportedModels': [],
        'commandsEncoding': 'Base64',
        'supportedController': 'Broadlink',
    }
    for f in PLATFORMS[platform]['fields']:
        if 'default' in f:
            cfg[f['key']] = copy.deepcopy(f['default'])
    cfg.setdefault('manufacturer', '')
    cfg['commands'] = {}
    return cfg


# ============================================================ command slots

def temp_range(cfg):
    lo = float(cfg.get('minTemperature', 16))
    hi = float(cfg.get('maxTemperature', 30))
    step = float(cfg.get('precision') or 1)
    out, t = [], lo
    while t <= hi + 1e-9:
        out.append(f'{t:g}')
        t += step
    return out


def build_slots(dev):
    """Return the ordered list of code slots for a device.

    Each slot is {path: [...keys], label, group}. `path` is where the code
    lives inside `commands`, so learning is fully generic across platforms.
    """
    p, cfg = dev['platform'], dev['config']
    slots = []

    def add(path, label, group):
        slots.append({'path': path, 'label': label, 'group': group})

    if p == 'climate':
        add(['off'], 'إطفاء', 'أساسي')
        if cfg.get('hasOn'):
            add(['on'], 'تشغيل', 'أساسي')
        temps = temp_range(cfg)
        presets = [x for x in (cfg.get('presetModes') or []) if x] or [None]
        for op in cfg.get('operationModes') or []:
            for preset in presets:
                for fan in cfg.get('fanModes') or ['']:
                    for sw in cfg.get('swingModes') or ['']:
                        parts = [op] + ([preset] if preset else [])
                        parts += [x for x in (fan, sw) if x]
                        group = ' / '.join(parts)
                        for t in temps:
                            add(parts + [t], f'{t}°', group)

    elif p == 'media_player':
        for key, label in [('on', 'تشغيل'), ('off', 'إطفاء'), ('mute', 'كتم'),
                           ('volumeUp', 'رفع الصوت'), ('volumeDown', 'خفض الصوت'),
                           ('nextChannel', 'القناة التالية'),
                           ('previousChannel', 'القناة السابقة')]:
            add([key], label, 'أزرار أساسية')
        for src in cfg.get('sources') or []:
            add(['sources', src], src, 'المصادر')

    elif p == 'fan':
        add(['on'], 'تشغيل', 'أساسي')
        add(['off'], 'إطفاء', 'أساسي')
        if cfg.get('hasOscillate'):
            add(['oscillate'], 'تدوير', 'أساسي')
        for d in cfg.get('directions') or ['forward']:
            for s in cfg.get('speed') or []:
                add([d, s], s, f'اتجاه {d}')

    elif p == 'light':
        add(['on'], 'تشغيل', 'أساسي')
        add(['off'], 'إطفاء', 'أساسي')
        add(['night'], 'وضع ليلي', 'أساسي')
        if cfg.get('hasStepped'):
            for key, label in [('brighten', 'أفتح'), ('dim', 'أغمق'),
                               ('colder', 'أبرد'), ('warmer', 'أدفأ')]:
                add([key], label, 'تدرّج')
        for b in cfg.get('brightness') or []:
            add(['brightness', str(b)], str(b), 'مستويات السطوع')
        for k in cfg.get('colorTemperature') or []:
            add(['colorTemperature', str(k)], str(k) + 'K', 'حرارة اللون')

    return slots


def get_at(commands, path):
    node = commands
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, str) else None


def set_at(commands, path, code):
    node = commands
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = code


def del_at(commands, path):
    node = commands
    for key in path[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            return
    node.pop(path[-1], None)


def device_stats(dev):
    slots = build_slots(dev)
    cmds = dev['config'].get('commands', {})
    done = sum(1 for s in slots if get_at(cmds, s['path']))
    return {'total': len(slots), 'done': done}


# ============================================================ project store

def _new_id():
    return uuid.uuid4().hex[:8]


def _migrate_legacy():
    """Seed the project from the original single-device smartir.json."""
    devices = []
    for path in (LEGACY_PATH, TEMPLATE_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        cfg.setdefault('commands', {})
        cfg.setdefault('supportedController', 'Broadlink')
        cfg.setdefault('commandsEncoding', 'Base64')
        name = cfg.get('manufacturer') or 'مكيّف'
        devices.append({'id': _new_id(), 'name': name, 'platform': 'climate', 'config': cfg})
        break
    return {'version': 1, 'devices': devices}


def load_project():
    if os.path.exists(PROJECT_PATH):
        with open(PROJECT_PATH, encoding='utf-8') as f:
            proj = json.load(f)
        proj.setdefault('devices', [])
        return proj
    proj = _migrate_legacy()
    save_project(proj)
    return proj


def save_project(proj):
    with open(PROJECT_PATH, 'w', encoding='utf-8') as f:
        json.dump(proj, f, indent=2, ensure_ascii=False)


def find_device(proj, device_id):
    for d in proj['devices']:
        if d['id'] == device_id:
            return d
    return None


def device_summary(dev):
    s = device_stats(dev)
    return {
        'id': dev['id'], 'name': dev['name'], 'platform': dev['platform'],
        'icon': PLATFORMS[dev['platform']]['icon'],
        'done': s['done'], 'total': s['total'],
    }


# ============================================================ API: platforms

@app.get('/api/platforms')
def api_platforms():
    return jsonify({k: {'label': v['label'], 'icon': v['icon'], 'fields': v['fields']}
                    for k, v in PLATFORMS.items()})


# ============================================================ API: devices

@app.get('/api/devices')
def api_devices():
    proj = load_project()
    return jsonify({'devices': [device_summary(d) for d in proj['devices']]})


@app.post('/api/devices')
def api_add_device():
    body = request.get_json(force=True)
    platform = body.get('platform')
    if platform not in PLATFORMS:
        return jsonify({'ok': False, 'error': 'Unknown platform'}), 400
    proj = load_project()
    dev = {
        'id': _new_id(),
        'name': (body.get('name') or '').strip() or PLATFORMS[platform]['label'],
        'platform': platform,
        'config': default_config(platform),
    }
    proj['devices'].append(dev)
    save_project(proj)
    return jsonify({'ok': True, 'device': device_summary(dev)})


@app.get('/api/devices/<device_id>')
def api_get_device(device_id):
    proj = load_project()
    dev = find_device(proj, device_id)
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    return jsonify({
        'device': dev,
        'slots': build_slots(dev),
        'stats': device_stats(dev),
        'spec': PLATFORMS[dev['platform']],
    })


@app.post('/api/devices/<device_id>')
def api_update_device(device_id):
    body = request.get_json(force=True)
    proj = load_project()
    dev = find_device(proj, device_id)
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    if 'name' in body:
        dev['name'] = (body['name'] or '').strip() or dev['name']
    incoming = body.get('config') or {}
    incoming.pop('commands', None)          # never clobber learned codes here
    dev['config'].update(incoming)
    save_project(proj)
    return jsonify({'ok': True, 'device': dev, 'slots': build_slots(dev),
                    'stats': device_stats(dev)})


@app.delete('/api/devices/<device_id>')
def api_delete_device(device_id):
    proj = load_project()
    before = len(proj['devices'])
    proj['devices'] = [d for d in proj['devices'] if d['id'] != device_id]
    if len(proj['devices']) == before:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    save_project(proj)
    return jsonify({'ok': True})


@app.post('/api/devices/<device_id>/duplicate')
def api_duplicate_device(device_id):
    proj = load_project()
    dev = find_device(proj, device_id)
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    copy_dev = copy.deepcopy(dev)
    copy_dev['id'] = _new_id()
    copy_dev['name'] = dev['name'] + ' (نسخة)'
    proj['devices'].append(copy_dev)
    save_project(proj)
    return jsonify({'ok': True, 'device': device_summary(copy_dev)})


# ============================================================ API: broadlink

@app.post('/api/discover')
def api_discover():
    body = request.get_json(silent=True) or {}
    try:
        found = broadlink.discover(timeout=int(body.get('timeout', 5)))
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
    devices = [{
        'ip': d.host[0],
        'mac': ':'.join(format(b, '02x') for b in reversed(d.mac)),
        'model': getattr(d, 'model', '') or '',
        'manufacturer': getattr(d, 'manufacturer', '') or '',
        'type': d.type,
    } for d in (found or [])]
    return jsonify({'ok': True, 'devices': devices})


@app.post('/api/connect')
def api_connect():
    global _device, _device_info
    ip = (request.get_json(force=True).get('ip') or '').strip()
    if not ip:
        return jsonify({'ok': False, 'error': 'IP address is required'}), 400
    try:
        dev = broadlink.hello(ip)
        dev.auth()
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), 502
    _device = dev
    _device_info = {
        'ip': ip,
        'model': getattr(dev, 'model', '') or '',
        'manufacturer': getattr(dev, 'manufacturer', '') or '',
        'mac': ':'.join(format(b, '02x') for b in reversed(dev.mac)),
        'type': dev.type,
    }
    return jsonify({'ok': True, 'device': _device_info})


@app.get('/api/status')
def api_status():
    return jsonify({'connected': _device is not None, 'device': _device_info})


# ============================================================ API: learning

@app.post('/api/learn')
def api_learn():
    """Learn one IR code and store it at every path given.

    Body: {deviceId, paths: [["cool","auto","auto","16"], ...]}
    """
    if _device is None:
        return jsonify({'ok': False, 'error': 'Not connected to a device'}), 409
    body = request.get_json(force=True)
    paths = body.get('paths') or []
    if not paths:
        return jsonify({'ok': False, 'error': 'No target path given'}), 400

    proj = load_project()
    dev = find_device(proj, body.get('deviceId'))
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404

    if not _lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Another learning session is in progress'}), 429
    try:
        _device.enter_learning()
        code, start = None, time.time()
        while time.time() - start < LEARN_TIMEOUT:
            time.sleep(0.5)
            try:
                code = base64.b64encode(_device.check_data()).decode('ascii')
                break
            except (ReadError, StorageError):
                continue
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), 502
    finally:
        _lock.release()

    if not code:
        return jsonify({'ok': False, 'error': 'No IR signal received within 30 seconds'}), 408

    commands = dev['config'].setdefault('commands', {})
    for path in paths:
        set_at(commands, path, code)
    save_project(proj)
    return jsonify({'ok': True, 'code': code, 'stats': device_stats(dev)})


@app.post('/api/send')
def api_send():
    """Replay a stored code so the user can verify it works."""
    if _device is None:
        return jsonify({'ok': False, 'error': 'Not connected to a device'}), 409
    body = request.get_json(force=True)
    proj = load_project()
    dev = find_device(proj, body.get('deviceId'))
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    code = get_at(dev['config'].get('commands', {}), body.get('path') or [])
    if not code:
        return jsonify({'ok': False, 'error': 'No code stored at that slot'}), 404
    try:
        _device.send_data(base64.b64decode(code))
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}), 502
    return jsonify({'ok': True})


@app.post('/api/clear')
def api_clear():
    """Delete one slot, or every slot in a group."""
    body = request.get_json(force=True)
    proj = load_project()
    dev = find_device(proj, body.get('deviceId'))
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    commands = dev['config'].setdefault('commands', {})
    for path in body.get('paths') or []:
        del_at(commands, path)
    save_project(proj)
    return jsonify({'ok': True, 'stats': device_stats(dev)})


# ============================================================ API: codes view

@app.post('/api/codes')
def api_codes():
    """Return which of the given slot paths currently hold a code."""
    body = request.get_json(force=True)
    proj = load_project()
    dev = find_device(proj, body.get('deviceId'))
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    commands = dev['config'].get('commands', {})
    out = []
    for path in body.get('paths') or []:
        out.append(get_at(commands, path))
    return jsonify({'ok': True, 'codes': out})


# ============================================================ API: export

def _safe_name(s):
    s = re.sub(r'[^\w؀-ۿ -]+', '', s or '').strip().replace(' ', '_')
    return s or 'device'


def smartir_file(dev):
    """Strip UI-only keys so the result is a valid SmartIR device file."""
    cfg = {k: v for k, v in dev['config'].items() if k not in UI_ONLY_KEYS}
    cfg.setdefault('commandsEncoding', 'Base64')
    cfg.setdefault('supportedController', 'Broadlink')
    commands = cfg.pop('commands', {})
    cfg['commands'] = commands
    return cfg


@app.get('/api/export/project')
def api_export_project():
    """The single combined file: every device in one document."""
    proj = load_project()
    out = {
        'version': 1,
        'devices': [{
            'id': d['id'], 'name': d['name'], 'platform': d['platform'],
            'smartir': smartir_file(d),
        } for d in proj['devices']],
    }
    buf = io.BytesIO(json.dumps(out, indent=2, ensure_ascii=False).encode('utf-8'))
    return send_file(buf, mimetype='application/json', as_attachment=True,
                     download_name='broadlink-devices.json')


@app.get('/api/export/device/<device_id>')
def api_export_device(device_id):
    proj = load_project()
    dev = find_device(proj, device_id)
    if not dev:
        return jsonify({'ok': False, 'error': 'No such device'}), 404
    payload = json.dumps(smartir_file(dev), indent=4, ensure_ascii=False).encode('utf-8')
    return send_file(io.BytesIO(payload), mimetype='application/json', as_attachment=True,
                     download_name=f'{_safe_name(dev["name"])}.json')


@app.get('/api/export/zip')
def api_export_zip():
    """SmartIR-ready layout: codes/<platform>/<name>.json, one file per device."""
    proj = load_project()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        used = set()
        for dev in proj['devices']:
            name = _safe_name(dev['name'])
            candidate, n = name, 2
            while (dev['platform'], candidate) in used:
                candidate, n = f'{name}_{n}', n + 1
            used.add((dev['platform'], candidate))
            z.writestr(f'codes/{dev["platform"]}/{candidate}.json',
                       json.dumps(smartir_file(dev), indent=4, ensure_ascii=False))
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='smartir-codes.zip')


@app.post('/api/import/project')
def api_import_project():
    """Restore a combined project file produced by /api/export/project."""
    data = request.get_json(force=True)
    if not isinstance(data, dict) or not isinstance(data.get('devices'), list):
        return jsonify({'ok': False, 'error': 'Not a valid project file'}), 400
    devices = []
    for d in data['devices']:
        platform = d.get('platform')
        if platform not in PLATFORMS:
            continue
        cfg = d.get('smartir') or d.get('config') or {}
        cfg.setdefault('commands', {})
        merged = default_config(platform)
        merged.update(cfg)
        devices.append({
            'id': d.get('id') or _new_id(),
            'name': d.get('name') or PLATFORMS[platform]['label'],
            'platform': platform,
            'config': merged,
        })
    if not devices:
        return jsonify({'ok': False, 'error': 'No usable devices in that file'}), 400
    save_project({'version': 1, 'devices': devices})
    return jsonify({'ok': True, 'devices': [device_summary(d) for d in devices]})


@app.post('/api/import/device')
def api_import_device():
    """Add a single existing SmartIR file as a new device."""
    body = request.get_json(force=True)
    platform = body.get('platform')
    cfg = body.get('config')
    if platform not in PLATFORMS or not isinstance(cfg, dict):
        return jsonify({'ok': False, 'error': 'Pick a platform and a valid JSON file'}), 400
    merged = default_config(platform)
    merged.update(cfg)
    merged.setdefault('commands', {})
    proj = load_project()
    dev = {
        'id': _new_id(),
        'name': body.get('name') or merged.get('manufacturer') or PLATFORMS[platform]['label'],
        'platform': platform,
        'config': merged,
    }
    proj['devices'].append(dev)
    save_project(proj)
    return jsonify({'ok': True, 'device': device_summary(dev)})


# ============================================================ static

@app.get('/')
def index():
    return send_from_directory(os.path.join(BASE_DIR, 'web'), 'index.html')


@app.get('/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'web'), filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8777))
    print(f'\n  Broadlink SmartIR Studio  ->  http://127.0.0.1:{port}\n')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
