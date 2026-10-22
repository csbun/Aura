"""Opt-in tag durability check on ONE explicitly selected untagged test asset.

A successful tag API response is insufficient: metadata refresh must preserve it.
No event migration may be enabled just because the tag CRUD checks pass.
"""
import time
from uuid import uuid4

from .immich import ImmichError


def _wait(predicate, timeout, sleep, clock):
    deadline = clock() + timeout
    while not predicate():
        if clock() >= deadline:
            raise ImmichError('标签持久性检查超时；不能判定通过')
        sleep(0.5)


def check_tag_persistence(client, asset_id, *, timeout=30, sleep=time.sleep, clock=time.monotonic):
    client.require_supported()
    original = client.asset_detail(asset_id)
    if original['tags']:
        raise ImmichError('只允许使用没有标签的独立测试照片，以免重新提取元数据影响已有标签')
    idle = lambda: client.jobs_idle(('sidecar', 'metadataExtraction'))
    _wait(idle, timeout, sleep, clock)
    marker = 'Aura tag persistence check ' + str(uuid4())
    tag_id = None
    result = None
    try:
        tag_id = client.create_tag(marker)['id']
        client.change_tag_assets(tag_id, {asset_id})
        _wait(idle, timeout, sleep, clock)
        before = client.asset_detail(asset_id)
        if tag_id not in {t['id'] for t in before['tags']}:
            raise ImmichError('测试标签未成功关联到照片')
        client.refresh_asset_metadata(asset_id)

        def refreshed():
            return idle() and client.asset_detail(asset_id)['updatedAt'] != before['updatedAt']

        _wait(refreshed, timeout, sleep, clock)
        after = client.asset_detail(asset_id)
        if after.get('checksum') != original.get('checksum'):
            raise ImmichError('测试照片校验和发生变化，请检查上游处理')
        persisted = tag_id in {t['id'] for t in after['tags']}
        result = {
            'metadata_refreshed': True,
            'tag_persisted': persisted,
            'migration_ready': persisted,
            'detail': '通过' if persisted else '元数据刷新后标签关联丢失；事件标签迁移不可启用。检查 XMP 写入能力。',
        }
    finally:
        # Recover a timed-out create by its unique probe marker; never delete a user tag.
        tags = client.tags()
        matches = [t for t in tags if t.get('name') == marker and not t.get('parentId')
                   and (tag_id is None or t['id'] == tag_id)]
        if tag_id is not None and any(t['id'] == tag_id for t in tags) and not matches:
            raise ImmichError('测试标签被改名或移动，未自动删除，请核对测试标签')
        for tag in matches:
            client.delete_tag(tag['id'])
    return result
