# Aura 0.4.3

Immich 管理照片、人物、标签和识别任务；Aura 管理**事件和动态相册规则**。只使用官方 API，不修改 Immich、不复制图库索引、不挂载原图。

事件名称和照片 ID 关联保存在 Aura，不写 Immich 标签或 XMP，也不自动创建相册。用户需要收藏或分享时，再通过事件或组合条件创建 Immich 相册。

## 安装：已有 Immich

需要 Docker 和 Compose。复制 `.env.example` 为 `.env`，执行 `chmod 600 .env`，填写：

```env
IMMICH_URL=http://<Immich的局域网IP>:2283
IMMICH_PUBLIC_URL=http://<浏览器可访问的Immich地址>:2283
IMMICH_API_KEY='图库用户的key'
AURA_PORT=18080
```

`IMMICH_URL` 必须能从 Aura 容器访问，不能用宿主机的 `127.0.0.1`。`IMMICH_PUBLIC_URL` 留空时使用浏览器当前主机名和 `IMMICH_PORT`。

在外部库所属用户下创建 API key，授予：`user.read`、`asset.read`、`asset.view`、`person.read`、`tag.read`、`album.read`、`album.create`、`albumAsset.create`、`albumAsset.delete`。不需要管理员任务 key 或标签写权限。旧版人物名称迁移另需 `person.update`；可选相册写入检查另需 `album.delete`。

```bash
docker compose up -d --build
```

打开 `http://<主机IP>:18080`，任务自动运行。

## 安装：同时部署 Immich + NAS

2GB 内存 NAS 只提供文件；应用、数据库和模型放在 Ubuntu 主机，原图通过 SMB **只读**提供给 Immich。

1. 主机安装 Docker、Compose 和 `cifs-utils`；NAS 启用 SMB，账号能读取照片共享。
2. 复制 `.env.example` 为 `.env`，执行 `chmod 600 .env`，填写：

   ```env
   NAS_HOST=你的NAS局域网IP
   NAS_SHARE=你的照片共享名
   NAS_USER=你的SMB用户名
   NAS_PASSWORD='SMB密码'
   IMMICH_DB_PASSWORD='随机字母数字密码'
   IMMICH_VERSION=v3.3.4
   IMMICH_URL=http://immich-server:2283
   ```

   `NAS_SHARE` 是 `smb://<NAS_IP>/<共享名>` 的共享名；密码含 `$` 时用单引号。
3. 启动：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.immich.yml \
     --profile immich up -d --build
   ```

4. 打开 `http://<主机IP>:2283` 初始化 Immich。在 **Administration → External Libraries** 创建外部库，选择图库用户，添加 `/photos` 并执行 Scan。
5. 登录该用户创建上述权限的 API key，填写 `.env` 的 `IMMICH_API_KEY`，再次执行：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.immich.yml \
     --profile immich up -d aura aura-worker
   ```

Immich 负责扫描、缩略图、人物和 Smart Search。新增照片的扫描周期在 Immich 设置。Aura 默认每天整理事件、每 5 分钟更新相册规则；用 `AURA_EVENT_INTERVAL`、`AURA_ALBUM_INTERVAL` 调整秒数。

## 使用

- **事件**：目前按日期/6 小时间隔生成，默认通过；可改名、合并或删除。人工改名或合并后停止自动扩充，删除后原成员不重新生成事件。
- **相册**：事件卡片点「创建相册」，预览后保存；也可自由组合人物、普通标签、事件、时间及地点。所有条件同时满足，多选事件取交集。创建规则后才在 Immich 生成相册，自动事件不混入用户相册列表。
- **删除事件**：照片及已有 Immich 相册保留，引用它的规则暂停；编辑条件后恢复。合并事件会自动改指向相关规则。
- **自然语言**：先解析为可编辑条件，再预览。画面描述交给 Immich Smart Search，取最相关的前 100 张，再与事件求交；中文效果取决于模型。
- **失效相册**：显示已删除/无权限提示，可删除 Aura 规则；这不会删除远端相册或照片。

事件只在 Aura 中整理；Immich 中改标签不会修改 Aura 事件。只读图库不影响 Aura 事件，但不能解决 Immich 自身编辑标签后的 XMP 持久性问题。

## 从 0.4.2 升级

先停旧 worker，再用上面的完整 Compose 命令重建。使用样本时保留 `-f docker-compose.samples.yml`。

启动后自动归档数据库到 `data/aura.pre-0.4.3.sqlite`，读取受管理标签的当前名称及完整成员，一次事务迁回事件并转换规则。保留事件 UUID 和人工调整；失败暂停并重试。旧版若仍有未完成标签操作，先用 0.4.2 完成或取消它们再升级。

`IMMICH_JOB_API_KEY`、`AURA_EVENT_TAGS_ENABLED` 已移除，可从 `.env` 删除。NAS 和样本挂载恢复只读；旧 `nas_photos_rw` 卷不再使用，不删除照片。已有 Immich 标签与 XMP 保留，迁回后不再同步；不批量删除可能含其他元数据的文件。详情见[迁移说明](docs/local-events.md)。

## 检查与排障

```bash
curl http://127.0.0.1:18080/health
curl http://127.0.0.1:18080/api/v1/tasks
docker compose logs -f aura-worker
docker compose exec aura aura-check
```

- 照片或人物为空：确认 key 所属用户能看到外部库，等待 Immich 相应任务完成。
- SMB 失败：检查共享名、账号和 TCP 445；只有 Immich 需要 SMB。
- 未验证版本、换实例或换 key 所属用户：自动任务暂停；新图库使用独立 `AURA_DATA_DIR`。
- 模型下载失败：确认主机能访问 Hugging Face；需要镜像时配置 `IMMICH_ML_HF_ENDPOINT` 后重建机器学习容器。

`data/` 保存事件和规则，`immich/` 保存 Immich 数据库及衍生文件；数据库使用主机本地盘。当前面向可信局域网，单用户 API key，无独立登录。

## 开发与计划

```bash
pip install -e '.[dev]'
pytest -q
```

接口文档：`http://<主机IP>:18080/docs`。跨日活动识别、人物/画面证据及有意义命名属于 [0.5](ROADMAP.md)，尚未实现。

样本：见[运行说明](docs/run-samples.md)及[准备流程](docs/album-samples.md)。当前 CITIES 500 张、PIPA 442 张；近似 GPS 和未标注活动边界的样本仅用于流程验证。
