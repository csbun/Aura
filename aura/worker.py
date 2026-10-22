from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from .config import ImmichConfig, Settings
from .db import Database
from .immich import ImmichClient, ImmichSettings
from .service import AuraService, now

LOGGER = logging.getLogger('aura.worker')


def run_task(database, name, action):
    database.execute("INSERT INTO task_status(name,state,started_at) VALUES (?,'running',?) ON CONFLICT(name) DO UPDATE SET state='running',started_at=excluded.started_at,finished_at=NULL,error=NULL", [name, now()])
    try:
        result = action()
        error = '; '.join(result.get('errors', [])) or None
    except Exception as exception:
        result, error = None, str(exception)
        LOGGER.exception('task failed: %s', name)
    database.execute('UPDATE task_status SET state=?,finished_at=?,result_json=?,error=? WHERE name=?', ['failed' if error else 'complete', now(), json.dumps(result, ensure_ascii=False), error, name])
    LOGGER.info('%s: %s', name, error or result)
    return error is None


def run_once(service, settings):
    database = service.database
    report = json.loads(database.get_setting('migration_report', 'null'))
    if report and not report.get('names_finished'):
        run_task(database, 'migration', service.finish_migration)
    if not run_task(database, 'event_tags', service.process_event_operations):
        return
    # Do not enumerate photo metadata every time an album is checked.
    last = database.get_setting('events_built_at')
    if not last or (datetime.fromisoformat(now()) - datetime.fromisoformat(last)).total_seconds() >= settings.event_interval:
        run_task(database, 'events', service.build_events)
    run_task(database, 'albums', service.sync_albums)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings, config = Settings.from_env(), ImmichConfig.from_env()
    service = AuraService(Database(settings.db_path), ImmichClient(ImmichSettings(config.url, config.api_key, config.job_api_key)))
    with service.database.lock('worker'):
        service.database.execute("UPDATE task_status SET state='interrupted',error='worker restarted' WHERE state='running'")
        while True:
            run_once(service, settings)
            time.sleep(settings.album_interval)
