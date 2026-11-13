import hashlib
import json
import re
import urllib.request

import coldfix as c

PATH = 'public-replay-admissions.json'


def identity(slug, repository_id, timeout=30):
    request = urllib.request.Request('https://api.github.com/repos/' + slug,
                                     headers={'Accept': 'application/vnd.github+json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        c.require(response.url == request.full_url, 'public repository redirected')
        value = json.load(response)
    c.require(value.get('private') is False and value.get('visibility') == 'public'
              and value.get('full_name') == slug and type(value.get('id')) is int
              and value['id'] == repository_id, 'public repository identity not verified')


def load(root):
    path = root / PATH
    if not path.exists():
        return {}
    value = json.loads(c.read_text(path), object_pairs_hook=c.unique_object)
    c.require(isinstance(value, dict) and set(value) == {'version', 'entries', 'digest'}
              and type(value['version']) is int and value['version'] == 1
              and isinstance(value['entries'], list), 'invalid public admission manifest')
    digest = hashlib.sha256(c.encoded(dict(version=1, entries=value['entries'])).encode()).hexdigest()
    c.require(value['digest'] == digest, 'public admission manifest digest mismatch')
    admissions = {}
    for entry in value['entries']:
        c.require(isinstance(entry, dict) and set(entry) == {'repo', 'repository_id', 'before', 'after'}
                  and isinstance(entry['repo'], str)
                  and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', entry['repo'])
                  and type(entry['repository_id']) is int and entry['repository_id'] > 0
                  and all(isinstance(entry[k], str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', entry[k])
                          for k in ('before', 'after')) and entry['before'] != entry['after']
                  and entry['repo'] not in admissions, 'invalid public admission binding')
        identity(entry['repo'], entry['repository_id'])
        admissions[entry['repo']] = dict(entry, manifest_digest=digest)
    return admissions
