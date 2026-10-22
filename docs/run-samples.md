# 用本地样本测试 Aura

Aura 只访问 Immich API，不读取照片路径。沿用现有 Immich，新增测试用户和样本库；测试 Aura 使用独立端口、数据库和 API key。

## 1. 挂载样本

本地目录是 `sample-photos/`，照片集为：`cities/` 500 张、`pipa/` 442 张，均为补充拍摄日期后的版本。配置为只读挂载，Aura 事件不生成 `.xmp`。不要混入原版副本、联系表或缓存。来源和元数据处理见 [样本说明](album-samples.md)。

在仓库根目录执行（会短暂重启 Immich 服务）：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  -f docker-compose.samples.yml --profile immich \
  up -d --no-deps immich-server
```

真实 NAS 挂载保留，额外增加 `/samples`。以后重建该服务时保留 `docker-compose.samples.yml`。这组配置将 NAS 与样本均设为只读；已有容器须重建后才应用。样本用户的 API key 仍只查询样本库。

## 2. 创建测试图库

1. 在 Immich 管理页面创建一个专用测试用户；不要与真实图库建立伙伴共享。
2. 管理员创建外部库，所有者选该测试用户，只添加 `/samples`，执行 Scan。Immich 会递归扫描全部子目录，无需按数据集分别配置。
3. 等待元数据、缩略图、人物和 Smart Search 任务完成。
4. 登录测试用户，创建 API key。权限与 [README](../README.md)一致；确认该用户首页只显示样本照片。

使用管理员/真实用户的 key 会查询其可见图库，无法实现测试隔离。Aura 当前没有按外部库路径筛选的配置。

## 3. 启动测试 Aura

若需要独立实例，新建 `.env.samples`（已加入 Git 忽略）；当前环境已将原 Aura 切到样本用户，可直接使用现有 `.env`，不必再启动一套。独立实例配置如下：

```env
AURA_PORT=18081
AURA_DATA_DIR=./data/samples
AURA_EVENT_INTERVAL=300
AURA_ALBUM_INTERVAL=60
IMMICH_URL=http://<主机局域网IP>:2283
IMMICH_PUBLIC_URL=http://<主机局域网IP>:2283
IMMICH_PORT=2283
IMMICH_API_KEY='测试用户的key'
```

这里的主机 IP 是运行 Immich 的 Ubuntu 主机，替换占位符，并按实际情况修改端口。独立 Compose 项目不共享原项目的内部 DNS，不能写 `http://immich-server:2283`；容器里的 `127.0.0.1` 也不是宿主机。

```bash
chmod 600 .env.samples
docker compose -p aura-samples --env-file .env.samples \
  -f docker-compose.yml up -d --build
```

打开 `http://<主机IP>:18081`。原 Aura 继续使用 `18080` 和原数据库；启动后自动整理样本，每 5 分钟更新事件、每分钟更新规则。

```bash
curl http://127.0.0.1:18081/api/v1/integrations/immich
docker compose -p aura-samples --env-file .env.samples \
  -f docker-compose.yml logs -f aura-worker
```

新增样本先让 Immich 扫描，之后 Aura 自动读取。测试完成可停掉独立 Aura（数据保留）：

```bash
docker compose -p aura-samples --env-file .env.samples \
  -f docker-compose.yml stop
```

## 数据边界

- 不要仅把原 `/photos` 从 NAS 换成本地目录：原 Immich 库仍记录真实照片，新挂载会让这些记录显示离线。
- 不要让真实/测试 Aura 共用数据库：事件引用具体 Immich 照片、人物和用户 ID。
- Aura 不读取样本旁的自定义 JSON；时间、地点须由 Immich 从 EXIF 等来源识别。CITIES 的补充 GPS 是城市代表点，适合流程测试，不代表真实拍摄位置；这些样本也不是已标注旅行边界的验收集。
