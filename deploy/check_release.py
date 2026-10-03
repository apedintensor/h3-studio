"""CPU release health only; never log cookies, provider config or user records."""
import json
import sys
import urllib.request

expected = sys.argv[1]
with urllib.request.urlopen('http://127.0.0.1:8844/healthz', timeout=5) as response:
    result = json.load(response)
assert result['status'] == 'ok'
assert result['release'] == expected
assert result['generation_enabled'] is False
assert result['authentication'] == 'password'
assert result['auth_ready'] is True
print('CPU release ready; password authentication enabled; GPU disabled')
