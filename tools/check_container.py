"""Smoke-test the production Linux image with disposable, synthetic accounts.

No network, provider credentials, model weights, production files or host mounts.
"""
import subprocess
import sys

SCRIPT = r'''
import io, json
from unittest import mock
from PIL import Image
from fastapi.testclient import TestClient
from password_auth import set_password
import server
password = 'Synthetic-CI-Password-Only-938!'
for username in ('superdan','supervan'):
    set_password(server.DB, username, password)
with mock.patch('httpx.HTTPTransport.handle_request',side_effect=AssertionError('No network')):
    with TestClient(server.app) as dan, TestClient(server.app) as van:
        health = dan.get('/healthz').json()
        assert health['status']=='ok' and health['authentication']=='password' and health['auth_ready']
        assert health['generation_enabled'] is False
        assert dan.get('/api/jobs').status_code==401
        assert dan.post('/api/auth/login',json={'username':'superdan'}).status_code in (401,422)
        assert dan.post('/api/auth/login',json={'username':'superdan','password':password}).status_code==200
        assert van.post('/api/auth/login',json={'username':'supervan','password':password}).status_code==200
        stream=io.BytesIO(); Image.new('RGB',(320,320),'orange').save(stream,format='PNG')
        upload=dan.post('/api/uploads',files={'file':('synthetic.png',stream.getvalue(),'image/png')})
        assert upload.status_code==200
        uid=upload.json()['id']
        assert van.get('/api/uploads/'+uid).status_code==422
        caps=dan.get('/api/capabilities').json()
        assert caps['backends'][0]['available'] is False and caps['expert_url'] is None
        assert dan.get('/openapi.json').status_code==200
print('Linux image smoke passed: password login, user isolation, CPU readiness, GPU disabled')
'''

def main():
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python tools/check_container.py h3-studio:<tag>')
    command=['docker','run','--rm','-i','--network','none','--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges',
        '--tmpfs','/data:uid=10001,gid=10001,mode=0700',
        '--tmpfs','/tmp:mode=1777',
        '-e','H3_STUDIO_DATA=/data','-e','H3_GENERATION_ENABLED=0',
        '-e','H3_AUTH_MODE=password','-e','H3_RELEASE=container-smoke',
        sys.argv[1],'python','-']
    subprocess.run(command,input=SCRIPT,text=True,check=True,timeout=120)

if __name__=='__main__':
    main()
