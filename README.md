# Aura 0.2

Aura 在 sidecar 上索引 NAS 照片，Immich 提供浏览和相册。原图通过 SMB 只读挂载，服务不会移动、删除或改写原图。

## 1. 准备

sidecar（Ubuntu/Debian x86_64）安装 Docker、Compose 和 CIFS 工具：

```bash
sudo apt update && sudo apt install -y cifs-utils
docker --version
docker compose version
```

fnOS：**设置 → 文件共享协议 → SMB**，给用户 `hans` 照片目录读取权限。若地址是 `smb://192.168.122.10/photos`，共享名就是 `photos`。

## 2. 配置

```bash
cp .env.example .env
chmod 600 .env
```

编辑至少这些值：

```env
NAS_HOST=192.168.122.10
NAS_SHARE=photos
NAS_USER=hans
NAS_PASSWORD='SMB密码'
NAS_SMB_VERSION=3.0
AURA_PORT=18080
AURA_SCAN_INTERVAL=86400

IMMICH_DB_PASSWORD='只含字母和数字的随机密码'
IMMICH_API_KEY=
IMMICH_PATH_PREFIX=/photos
```

密码含 `$` 时使用单引号。`.env` 不要提交到 Git。

## 3. 启动

### Aura + Immich（推荐）

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich up -d --build
```

Aura 在 `18080`，Immich 在 `2283`。两者都把 SMB 挂载为 `/photos`。

### 仅 Aura

```bash
docker compose -f docker-compose.yml -f docker-compose.smb.yml \
  up -d --build
```

## 4. 自动任务

启动后 `aura-worker` 会自动：

- 立即扫描照片；
- 生成事件候选；
- 配置 Immich API key 后，自动映射资产和同步智能相册；
- 扫描按 `AURA_SCAN_INTERVAL` 重复执行（默认 24 小时）；相册约每分钟同步，失败自动重试。

无需手工运行扫描、事件或同步命令。查看进度：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich logs -f aura-worker
```

修改周期后重建 worker：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich up -d --build aura-worker
```

## 5. Immich 只需配置一次外部库

1. 打开 `http://<sidecar-IP>:2283`，完成初始化。
2. **Administration → External Libraries → Create**。
3. 选择将用于 API key 的同一个用户。
4. 添加路径 `/photos`，点击 **Scan**，等待任务完成。
5. 在该用户设置中创建 API key，写入 `.env`：

   ```env
   IMMICH_API_KEY='API key'
   ```

更新 Aura 和 worker，使其读取 API key：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich up -d --build aura aura-worker
```

之后资产映射和智能相册同步自动执行。检查：

```bash
curl http://127.0.0.1:18080/api/v1/integrations/immich
```

外部库必须和 API key 属于同一用户，否则可能得到 `remote_assets: 0`。

## 6. 审核与智能相册

打开 `http://<sidecar-IP>:18080`：查看任务状态 → 预览事件照片 → 命名并确认 → 选择人物、事件、标签和时间创建相册。多个条件同时满足才会入选。

审核决定会保留；智能相册自动同步到 Immich。当前仅追加照片，人物识别和自动标签接入见路线图 0.3。

也可通过 API 创建（`event` 替换为实际事件 ID）：

```bash
curl -X POST http://127.0.0.1:18080/api/v1/smart-albums \
  -H 'Content-Type: application/json' \
  -d '{"name":"上海活动","query":{"event":1}}'
```

首次升级会自动迁移数据库，已有事件和相册保留。

## 7. 检查和排错

```bash
curl http://127.0.0.1:18080/health
curl http://127.0.0.1:2283/api/server/ping

docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich ps

docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich exec immich-server sh -lc 'find /photos -type f | head'
```

- `Cannot GET /api/server-info`：当前健康接口是 `/api/server/ping`。
- `remote_assets: 0`：检查 `/photos` 外部库是否扫描完成，以及 API key 用户是否一致。
- SMB 失败：检查 `cifs-utils`、共享名、账号权限和 TCP 445。

## 8. 数据和升级

运行数据位于 `./data`、`./immich/postgres` 和 `./immich/library`；配置在 `.env`。升级时保留这些目录，数据库不要放在 SMB 上。

`IMMICH_VERSION` 初期可用 `release`；稳定后改为已验证的具体版本。

## API

```text
GET  /health
GET  /api/v1/tasks
GET  /api/v1/smart-albums
PATCH /api/v1/events/{id}
GET  /api/v1/tag-assignments
PATCH /api/v1/assets/{asset_id}/tags/{tag_id}
GET  /api/v1/assets
GET  /api/v1/search/parse
GET  /api/v1/people
GET  /api/v1/events
GET  /api/v1/tags
POST /api/v1/smart-albums
GET  /api/v1/integrations/immich
POST /api/v1/integrations/immich/sync-assets
POST /api/v1/smart-albums/{id}/materialize
```
