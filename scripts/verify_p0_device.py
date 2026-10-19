#!/usr/bin/env python3
"""Interactive acceptance on the configured iPhone; never records phone content."""
import argparse
import datetime
import json
from pathlib import Path
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from iphone_use import Runtime, VERSION
from wda_client import WDAError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-id', required=True)
    parser.add_argument('--ready-label', required=True, help='Known noneditable label on the authenticated page')
    args = parser.parse_args()
    runtime = Runtime()
    evidence = {'version': VERSION, 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    output = Path(__file__).resolve().parents[1] / 'docs/p0-device-acceptance.json'
    def record(key, value):
        evidence[key] = value
        print(json.dumps({key: value}, ensure_ascii=False), flush=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    try:
        ready = runtime.call('pua_ready', {'screenshot': False})
        assert ready['ready']
        record('ready_before', {'ready': True, 'proof': ready['proof']})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            preview = runtime.screen.frame()
            if preview.get('frame'):
                record('mjpeg', {'received': True, 'width': preview['frame']['width'], 'height': preview['frame']['height']})
                break
            time.sleep(.2)
        else:raise RuntimeError('No live MJPEG frame')
        status = runtime.client.request('GET', '/status')['value']
        network = status['iphoneUseNetwork']
        assert all(x.get('observedLocalAddress') == '127.0.0.1' and x.get('observedLocalPort') == x['port'] for x in network['listeners'])
        record('network', network)
        lan_ip = status.get('ios', {}).get('ip')
        probes = []
        if lan_ip:
            for port in (8100, 9100):
                try:
                    with socket.create_connection((lan_ip, port), timeout=3):connected = True
                except OSError:connected = False
                probes.append({'family': 'IPv4', 'port': port, 'connected': connected})
        record('lan_probes', {'results': probes, 'limitation': 'The same IPv4 probes timed out before the patch. No reachable phone LAN IPv6 address was established; native transport IPv6 exclusion was tested separately.'})
        runtime.call('pua_screen', {'action': 'pause'})
        count = len(runtime.client.records)
        blocked = []
        for name, args in [('pua_observe', {'mode': 'tree'}), ('pua_observe', {'mode': 'screenshot'}),
                           ('pua_press_button', {'name': 'home'}),
                           ('pua_batch', {'steps': [{'op': 'observe', 'args': {}}]}),
                           ('pua_ready', {'screenshot': False}), ('pua_screen_action', {'action': 'refresh'})]:
            try:runtime.call(name, args)
            except WDAError as exc:
                assert exc.code == 'access_paused'
                blocked.append(name)
            else:raise RuntimeError('Pause did not reject ' + name)
        assert len(runtime.client.records) == count
        record('pause_gate', {'blocked': blocked, 'new_phone_requests': 0})
        result = runtime.call('pua_screen', {'action': 'resume'})
        assert result['paused'] and not result['confirmation_required']
        record('legacy_resume_has_no_mac_confirmation', True)
        print('Complete authentication on the iPhone. No Mac confirmation is needed.', flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = runtime.call('pua_screen', {'action': 'wait_auth', 'bundle_id': args.bundle_id,
                                                'ready_label': args.ready_label, 'timeout': 25})
            assert not result['confirmation_required']
            if not result['paused']: break
            if result['state'] not in ('waiting_auth',):
                raise RuntimeError('Authentication probe did not complete: ' + result['state'])
        assert not runtime.screen.paused(), 'Phone authentication has not been verified'
        record('phone_evidence_resumes', True)
        result = runtime.call('pua_ready', {'screenshot': False})
        assert result['ready']
        record('ready_after', {'ready': True, 'proof': result['proof'], 'preview': result['preview']})
        record('completed', True)
    finally:runtime.close()


if __name__ == '__main__':main()
