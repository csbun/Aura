"""Explicit compatibility check against a running official Immich instance."""
import argparse
import json
from uuid import uuid4

from .config import ImmichConfig
from .immich import ImmichClient, ImmichSettings, ImmichError


def check(client, write_test=False):
    version = client.require_supported()
    client.identity()
    people, tags, assets = client.people(), client.tags(), client.search({})
    if assets:
        client.thumbnail(assets[0]['id'])
    if people:
        client.search({'person_ids': [people[0]['id']]})
    if tags:
        client.search({'tag_ids': [tags[0]['id']]})
    client.search({'q': 'beach'})
    client.albums()
    result = {'version': version, 'photos': len(assets), 'people': len(people), 'tags': len(tags),
              'read_checks': 'passed', 'write_checks': 'not requested'}
    if write_test:
        marker = 'Aura compatibility check ' + str(uuid4())
        album_id = None
        try:
            album_id = client.create_album('Aura compatibility check (temporary)', marker)
            ids = {a['id'] for a in assets[:1]}
            client.change_album_assets(album_id, ids)
            if client.album_assets(album_id) != ids:
                raise ImmichError('Compatibility check: album additions were not visible')
            client.change_album_assets(album_id, ids, remove=True)
            if client.album_assets(album_id):
                raise ImmichError('Compatibility check: album removals were not visible')
            result['write_checks'] = 'passed' if ids else 'empty library; membership checks skipped'
        finally:
            # Only delete this uniquely marked test album; never assets or user albums.
            matches = [a for a in client.albums() if a.get('description') == marker]
            for album in matches:
                client.delete_check_album(album['id'], marker)
    return result


def main():
    parser = argparse.ArgumentParser(description='Check the configured Immich contract; does not change version allowlists')
    parser.add_argument('--write-test', action='store_true', help='Create a temporary album, test member add/remove, then delete only that album')
    args = parser.parse_args()
    config = ImmichConfig.from_env()
    try:
        result = check(ImmichClient(ImmichSettings(config.url, config.api_key)), args.write_test)
    except ImmichError as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
