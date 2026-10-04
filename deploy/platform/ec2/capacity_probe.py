"""Synthetic HTTP/media acceptance inside an isolated 2-GiB Linux container.

Not a production benchmark. No provider, model, host secrets or user files.
Run as UID 10001, network=none; writable /tmp and /results only.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/app')
from fastapi.testclient import TestClient
from PIL import Image
from studio_platform.api import create_app
from studio_platform.settings import Settings


def cgroup(name):
    path = Path('/sys/fs/cgroup')/name
    if path.is_file():
        return path.read_text().strip()
    legacy = Path('/sys/fs/cgroup/memory')
    if name == 'memory.events':
        return json.dumps({key: (legacy/key).read_text().strip()
                           for key in ('memory.failcnt', 'memory.oom_control')})
    return (legacy/{'memory.max': 'memory.limit_in_bytes',
                    'memory.peak': 'memory.max_usage_in_bytes'}[name]).read_text().strip()


def project(ident):
    return {'schemaVersion': 4, 'id': ident, 'title': 'Synthetic EC2 capacity',
            'logline': '', 'entities': [], 'links': [], 'jobs': [],
            'layout': {'positions': {}, 'viewport': {'x': 0, 'y': 0, 'zoom': 1}}}


def main():
    signal.alarm(600)
    assert os.getuid() == 10001
    assert int(cgroup('memory.max')) == 2*1024**3
    assert sorted(x.name for x in Path('/sys/class/net').iterdir()) == ['lo']
    report = {'memory_limit_bytes': int(cgroup('memory.max')), 'provider_calls': 0,
              'model_generation': False, 'scope': 'two-account initial CPU control plane',
              'phases': [], 'status': 'running', 'memory_events_before': cgroup('memory.events')}
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='ec2-capacity-') as directory:
        root = Path(directory)
        output = Path('/results/ec2-2g-capacity.json')
        try:
            # Solid synthetic video keeps input/output file bytes bounded while
            # exercising the full 5760-square decoder/encoder frame dimensions.
            video = root/'max-square-15s.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-f', 'lavfi', '-i', 'color=c=navy:s=5760x5760:r=24:d=15',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=15',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-x264-params', 'ref=1:bframes=0:rc-lookahead=0:sync-lookahead=0',
                '-threads', '2', '-c:a', 'aac', '-shortest', '-y', str(video)],
                check=True, capture_output=True, timeout=180)
            assert video.stat().st_size < 512*1024**2
            picture = root/'max-square.png'
            with Image.new('RGB', (5760, 5760), 'navy') as image:
                image.save(picture)
            app = create_app(Settings(root/'app', auth_mode='local-test'))
            with TestClient(app) as dan, TestClient(app) as van:
                for client, owner in ((dan, 'superdan'), (van, 'supervan')):
                    assert client.post('/api/auth/login', json={'username': owner}).status_code == 200
                    assert client.post('/v1/projects', json={'project': project('capacity')}).status_code == 201
                began = time.perf_counter()
                with video.open('rb') as stream:
                    response = dan.post('/v1/assets', data={'client_project_id': 'capacity',
                        'client_asset_id': 'capacity:video'}, files={'file': (video.name, stream, 'video/mp4')})
                assert response.status_code == 201, 'video_http_rejected'
                asset = response.json()
                asset = asset.get('asset', asset)
                assert asset['status'] == 'ready'
                report['phases'].append({'name': 'max_5760_square_15s_video_http_upload',
                    'seconds': round(time.perf_counter()-began, 3), 'status': asset['status'],
                    'metadata': asset['metadata']})

                def image_upload(pair):
                    client, owner = pair
                    with picture.open('rb') as stream:
                        response = client.post('/v1/assets', data={'client_project_id': 'capacity',
                            'client_asset_id': 'capacity:image'}, files={'file': (picture.name, stream, 'image/png')})
                    assert response.status_code == 201, 'image_http_rejected'
                    asset = response.json()
                    asset = asset.get('asset', asset)
                    assert asset['status'] == 'ready'
                    return {'owner': owner, 'status': asset['status'], 'metadata': asset['metadata']}
                began = time.perf_counter()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    uploaded = list(pool.map(image_upload, ((dan, 'superdan'), (van, 'supervan'))))
                report['phases'].append({'name': 'two_concurrent_5760_square_image_uploads',
                    'seconds': round(time.perf_counter()-began, 3), 'assets': uploaded})
                began = time.perf_counter()
                for n in range(50):
                    for client in (dan, van):
                        response = client.post('/v1/projects', json={'project': project('project-'+str(n))})
                        assert response.status_code == 201
                for client in (dan, van):
                    assert len(client.get('/v1/projects?limit=100').json()['projects']) == 51
                    h = client.get('/healthz')
                    assert h.status_code == 200 and not h.json()['generation_enabled']
                report['phases'].append({'name': 'two_accounts_50_additional_projects_each',
                    'seconds': round(time.perf_counter()-began, 3), 'projects_per_owner': 51})
            report['status'] = 'succeeded'
        except Exception as error:
            report['status'] = 'failed'
            report['error_type'] = type(error).__name__
            raise
        finally:
            report['seconds'] = round(time.perf_counter()-start, 3)
            report['memory_peak_bytes'] = int(cgroup('memory.peak'))
            report['memory_events_after'] = cgroup('memory.events')
            report['source_hashes'] = {name: hashlib.sha256((Path('/app/studio_platform')/name).read_bytes()).hexdigest()
                for name in ('api.py', 'media.py', 'media_process.py', 'assets.py')}
            output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            print(json.dumps({k: report[k] for k in ('status', 'seconds', 'memory_peak_bytes', 'memory_events_after')}))


if __name__ == '__main__':
    main()
