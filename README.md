# Aura 0.4.2（待完成环境验收）

Immich 管理照片、缩略图、人物、标签和识别任务。Aura 只做**事件整理、组合查询和动态相册规则**，不修改 Immich 源码、不访问其数据库、不扫描或挂载原图。

日常浏览、人物命名和纠正在 Immich 完成；在 Aura 配置规则，然后回 Immich 看相册。事件名称和成员也改存 Immich 标签。

当前运行环境仍保留 0.4.1 的 199 个事件；0.4.2 代码已实现，等待队列读取权限和完整迁移验收。升级前先满足下述事件标签条件。

## 安装：已有 Immich

需要 Docker 和 Compose。在 Immich 外部库所属用户下创建 API key，复制 `.env.example` 为 `.env`（`chmod 600 .env`），填写：

```env
IMMICH_URL=http://<Immich的局域网IP>:2283
IMMICH_PUBLIC_URL=http://<浏览器可访问的Immich地址>:2283
IMMICH_API_KEY='图库用户的key'
IMMICH_JOB_API_KEY='管理员的只读任务key'
AURA_EVENT_TAGS_ENABLED=false
AURA_PORT=18080
```

`IMMICH_URL` 必须能从 Aura 容器访问，不能用宿主机的 `127.0.0.1`。`IMMICH_PUBLIC_URL` 留空时使用当前浏览器主机名和 `IMMICH_PORT`。

API key 权限：`user.read`、`asset.read`、`asset.view`、`person.read`、`tag.read`、`album.read`、`album.create`、`albumAsset.create`、`albumAsset.delete`。事件写入还需 `tag.create`、`tag.update`、`tag.delete`、`tag.asset`、`asset.update`、`job.create`。完整权限 key 可继续用；迁移旧人物名称还需 `person.update`，执行写入兼容测试时还需 `album.delete`。

```bash
docker compose up -d --build
```

打开 `http://<主机IP>:18080`。无需手动运行扫描任务。

### 启用事件标签（必需前置条件）

1. Immich 必须能持久写入可见图库的 XMP；只读外部库暂不支持此功能。本地样本使用[可写配置](docs/run-samples.md)，NAS 默认仍只读。
2. 管理员创建仅有 `job.read` 权限的 key，填写 `IMMICH_JOB_API_KEY`。它只读取队列，不用于查询图库；图库仍由 `IMMICH_API_KEY` 隔离。若图库 key 本身已有管理员任务读取权限，可省略第二个 key。
3. 用一张无标签的独立测试照片执行 `docker compose exec aura aura-check --tag-test-asset <照片UUID>`。队列失败、权限不足或刷新后标签丢失均不能视为通过。
4. 检查通过后设置 `AURA_EVENT_TAGS_ENABLED=true` 并重新创建 Aura 和 worker。迁移及后续整理自动运行；无需人工执行扫描或迁移命令。

连续标签操作会等待 Sidecar 和元数据队列完成，防止旧 XMP 覆盖新操作。原有事件实例升级时，应先在测试实例完成此检查，避免旧事件迁移等待期间不可用。

## 安装：同时部署 Immich + NAS 外部库

2GB 内存 NAS 只提供文件；整套应用、数据库和模型放在 Ubuntu 主机。原图不复制，通过 SMB 只读提供给 Immich。

1. 主机安装 Docker、Compose 和 `cifs-utils`；NAS 启用 SMB 并授予照片目录读取权限。
2. 复制 `.env.example` 为 `.env`，`chmod 600 .env`，填写：

   ```env
   NAS_HOST=你的NAS局域网IP
   NAS_SHARE=你的照片共享名
   NAS_USER=你的SMB用户名
   NAS_PASSWORD='SMB密码'
   IMMICH_DB_PASSWORD='随机字母数字密码'
   IMMICH_VERSION=v3.3.4
   IMMICH_URL=http://immich-server:2283
   ```

   `NAS_SHARE` 是 `smb://<NAS_IP>/<共享名>` 中的共享名。密码含 `$` 用单引号。
3. 启动：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.immich.yml \
     --profile immich up -d --build
   ```

4. 打开 `http://<主机IP>:2283` 初始化 Immich。在 **Administration → External Libraries** 创建外部库，选择使用 API key 的同一用户，添加 `/photos`，执行 Scan。
5. 在该用户下创建上述权限的 API key，写入 `.env` 的 `IMMICH_API_KEY`，再运行：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.immich.yml \
     --profile immich up -d aura aura-worker
   ```

Immich 负责外部库扫描、缩略图、人物和语义索引。新增照片的扫描周期在 Immich 外部库设置中管理。满足事件标签前置条件后，Aura 自动生成事件，默认每天更新；相册规则默认每 5 分钟执行，可通过 `AURA_EVENT_INTERVAL`、`AURA_ALBUM_INTERVAL` 调整（秒）。

## 使用

- **事件**：按拍摄日期和 6 小时间隔自动整理，默认通过，以 `事件/<名称>` 标签保存在 Immich。勾选多个事件可合并（照片去重、相册规则自动改指向）；可按名称或日期查找。
- **删除事件**：不删除照片或 Immich 相册，原成员不会再次生成事件；相关规则暂停，编辑规则改选事件后恢复。自动事件可补入新照片；在 Aura 或 Immich 人工改名、改成员、合并后停止自动扩充。普通标签不会被接管。
- **查询**：人物、标签、事件、时间、城市/国家可组合，条件全部满足才入选。可先输入自然语言，再核对解析后的条件。
- **语义搜索**：画面描述交给 Immich，事件转换为标签过滤，与其他条件一起提交，取最相关的前 100 张。需 Immich Smart Search 索引；中文效果取决于其模型。这不是完整的自然语言推理。
- **相册**：先预览再保存，自动写入独立的 Immich 相册。“与规则一致”会增减成员；“只追加”保留旧成员。移出相册不删除照片；不要手工编辑规则相册的成员。规则名称只用于首次创建远端相册。“删除规则”会停止自动更新并移除 Aura 规则，保留照片和 Immich 相册。

人物/标签列表实时来自 Immich；Aura 不再生成本地“年份/横图”等标签。不识别的名称或重名请手工选定；地点名称须与 Immich 一致。日期筛选使用 UTC，结束日期不含当天。

## 从 0.4 / 0.4.1 升级

先满足事件标签前置条件，再停止旧 worker 并重建。仍只读或缺少队列权限时暂缓升级。

```bash
docker compose stop aura aura-worker
docker compose up -d --build --no-deps aura aura-worker
```

启动自动归档 `data/aura.pre-0.4.2.sqlite`，逐项创建标签、核验成员、转换规则；全部成功才删除旧运行表。失败任务保留并自动重试，页面可查看错误。取消普通失败任务会保留已完成的远端变更；旧数据迁移任务不可取消。归档仅是旧 Aura 数据库，不是原图备份。

同时部署 Immich 时使用原有 compose 文件组合；样本可写覆盖文件须继续保留。迁移后事件 API 的 ID 为 Immich 标签 UUID，规则保存 `tag_ids`，不再保存本地事件编号。

## 从 0.3 升级

先停止旧 Aura（避免旧 worker 继续写入旧数据库），保留 `.env` 和数据目录，将 `IMMICH_VERSION` 固定为 `v3.3.4`：

```bash
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich stop aura aura-worker
docker compose -f docker-compose.yml -f docker-compose.immich.yml \
  --profile immich up -d --build --no-deps aura aura-worker
```

首次启动自动归档 `data/aura.pre-0.4.sqlite` 并迁移：保留事件 ID、审核、相册 ID；照片/人物/标签条件改用 Immich ID。归档仅是 Aura 旧数据库，不是第二份原图。

旧相册继续“只追加”。旧人物名称仅在 Immich 对应人物仍未命名时自动补入，成功后恢复相关规则。名称冲突、本地审核或无法映射的条件写入迁移记录，相关规则暂停；在页面查看旧记录、到 Immich 核对，预览并保存规则后恢复。已有 Immich 名称不会被覆盖。 完整旧资料留在归档，迁移记录见 `/api/v1/migration`。

`AURA_SCAN_INTERVAL`、`PHOTO_ROOT`、`IMMICH_PATH_PREFIX`、`aura-scan` 和旧扫描/人物审核/资产映射 API 已移除；不再使用 `docker-compose.smb.yml`。仅接已有 Immich 时使用基础 compose 文件即可。

## 检查与升级兼容性

当前验证 **Immich 3.3.4**，使用官方镜像。不要配置浮动 `release` 标签。未经验证的版本会暂停自动任务和相册写入；规则仍可查看，事件读取依赖 Immich。

```bash
curl http://127.0.0.1:18080/api/v1/integrations/immich
docker compose logs -f aura-worker
docker compose exec aura aura-check
# 可选：临时创建一个测试相册，验证添加/移出成员并清理该相册
docker compose exec aura aura-check --write-test
```

同时部署 Immich 时，上述 compose 命令追加 `-f docker-compose.yml -f docker-compose.immich.yml --profile immich`（放在 `logs/exec` 前）。

升级流程：阅读 [Immich 发布说明与升级指南](https://docs.immich.app/install/upgrading/) → 在测试实例验证新版本的接口与写入行为 → 更新 Aura 适配器、测试和版本白名单 → 固定新镜像版本再部署。不是修改环境变量就能绕过兼容检查。实现细节见 [架构说明](docs/architecture.md)。

- 连接失败或无照片：检查 API key 权限、外部库用户、Immich 任务是否完成。
- 规则提示 Immich 相册已删除或无权限：检查原相册和 API key，或点击“删除规则”清理失效项；需要重新建相册时新建规则。
- 人物/语义结果为空：在 Immich 检查识别和 Smart Search；队列空不等于模型成功。模型源不可达时可设置 `IMMICH_ML_HF_ENDPOINT=https://hf-mirror.com`，重建机器学习容器。
- SMB 权限失败：检查共享名、账号、TCP 445；只有 Immich 需要 SMB。
- 原有实例改地址或 API key 换用户会暂停任务，避免把旧规则写到别的图库。新实例使用独立 `AURA_DATA_DIR`。

运行数据：`data/` 保存规则、受管理标签 ID、已处理照片 ID 和任务状态；`immich/` 保存 Immich 数据库及衍生文件。不要删除或放到 SMB 数据库目录。仅面向可信局域网，Aura 当前使用单用户 API key，无独立登录。

## 迭代范围

0.4.2 完成标签存储和现有整理操作的迁移；跨日活动识别、人物/画面判断及有意义的自动命名属于 0.5。环境验收状态见[持久性验证](docs/event-tag-persistence.md)。

## 开发

```bash
pip install -e '.[dev]'
pytest -q
```

HTTP API 文档：`http://<主机IP>:18080/docs`。路线见 [ROADMAP](ROADMAP.md)。

个人相册测试图片：见 [CITIES / PIPA 完整处理流程](docs/album-samples.md)。运行 `.venv/bin/python scripts/prepare_album_samples.py --count 500`，自动下载、恢复日期、补入城市近似坐标、无损写入 EXIF，并清理原图和缓存，只保留改写后的照片。PIPA 日期缺失或匹配不确定的照片会跳过；本次最终保留 CITIES 500 张、PIPA 442 张。
